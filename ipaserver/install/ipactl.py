# Authors: Simo Sorce <ssorce@redhat.com>
#
# Copyright (C) 2008-2019  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from __future__ import print_function

import sys
import os
import json

import ldapurl

from ipaserver.install import service, installutils
from ipaserver.install.dsinstance import config_dirname
from ipaserver.install.installutils import ScriptError
from ipaserver.masters import ENABLED_SERVICE, HIDDEN_SERVICE
from ipalib import api, errors
from ipalib.facts import is_ipa_configured
from ipapython.ipaldap import LDAPClient, realm_to_serverid
from ipapython.ipautil import wait_for_open_ports, wait_for_open_socket
from ipapython.ipautil import run
from ipapython import config
from ipaplatform.tasks import tasks
from ipapython.dn import DN
from ipaplatform import services
from ipaplatform.paths import paths

MSG_HINT_IGNORE_SERVICE_FAILURE = (
    "Hint: You can use --ignore-service-failure option for forced start in "
    "case that a non-critical service failed"
)


class IpactlError(ScriptError):
    pass


def check_IPA_configuration():
    if not is_ipa_configured():
        # LSB status code 6: program is not configured
        raise IpactlError(
            "IPA is not configured "
            "(see man pages of ipa-server-install for help)",
            6,
        )


def deduplicate(lst):
    """Remove duplicates and preserve order.
    Returns copy of list with preserved order and removed duplicates.
    """
    new_lst = []
    s = set(lst)
    for i in lst:
        if i in s:
            s.remove(i)
            new_lst.append(i)

    return new_lst


def is_dirsrv_debugging_enabled():
    """
    Check the 389-ds instance to see if debugging is enabled.
    If so we suppress that in our output.

    returns True or False
    """
    debugging = False
    serverid = realm_to_serverid(api.env.realm)
    dselist = [config_dirname(serverid)]
    for dse in dselist:
        try:
            fd = open(dse + "dse.ldif", "r")
        except IOError:
            continue
        lines = fd.readlines()
        fd.close()
        for line in lines:
            if line.lower().startswith("nsslapd-errorlog-level"):
                _option, value = line.split(":")
                if int(value) > 0:
                    debugging = True

    return debugging


def get_capture_output(service, debug):
    """
    We want to display any output of a start/stop command with the
    exception of 389-ds when debugging is enabled because it outputs
    tons and tons of information.
    """
    if service == "dirsrv" and not debug and is_dirsrv_debugging_enabled():
        print("    debugging enabled, suppressing output.")
        return True
    else:
        return False


def parse_options():
    usage = "%prog start|stop|restart|status\n"
    parser = config.IPAOptionParser(
        usage=usage, formatter=config.IPAFormatter()
    )

    parser.add_option(
        "-d",
        "--debug",
        action="store_true",
        dest="debug",
        help="Display debugging information",
    )
    parser.add_option(
        "-f",
        "--force",
        action="store_true",
        dest="force",
        help="Force IPA to start. Combine options "
        "--skip-version-check and --ignore-service-failures",
    )
    parser.add_option(
        "--ignore-service-failures",
        action="store_true",
        dest="ignore_service_failures",
        help="If any service start fails, do not rollback the "
        "services, continue with the operation",
    )
    parser.add_option(
        "--skip-version-check",
        action="store_true",
        dest="skip_version_check",
        default=False,
        help="skip version check",
    )

    options, args = parser.parse_args()
    safe_options = parser.get_safe_opts(options)

    if options.force:
        options.ignore_service_failures = True
        options.skip_version_check = True

    return safe_options, options, args


def emit_err(err):
    sys.stderr.write(err + "\n")


def version_check():
    try:
        installutils.check_version()
    except (
        installutils.UpgradeMissingVersionError,
        installutils.UpgradeDataOlderVersionError,
    ) as exc:
        emit_err("IPA version error: %s" % exc)
    except installutils.UpgradeVersionError as e:
        emit_err("IPA version error: %s" % e)
    else:
        return

    emit_err(
        "Automatically running upgrade, for details see {}".format(
            paths.IPAUPGRADE_LOG
        )
    )
    emit_err("Be patient, this may take a few minutes.")

    # Fork out to call ipa-server-upgrade so that logging is sane.
    result = run(
        [paths.IPA_SERVER_UPGRADE], raiseonerr=False, capture_error=True
    )
    if result.returncode != 0:
        emit_err("Automatic upgrade failed: %s" % result.error_output)
        emit_err(
            "See the upgrade log for more details and/or run {} again".format(
                paths.IPA_SERVER_UPGRADE
            )
        )
        raise IpactlError("Aborting ipactl")


def get_config(dirsrv):
    base = DN(
        ("cn", api.env.host),
        ("cn", "masters"),
        ("cn", "ipa"),
        ("cn", "etc"),
        api.env.basedn,
    )
    srcfilter = LDAPClient.combine_filters(
        [
            LDAPClient.make_filter({"objectClass": "ipaConfigObject"}),
            LDAPClient.make_filter(
                {"ipaConfigString": [ENABLED_SERVICE, HIDDEN_SERVICE]},
                rules=LDAPClient.MATCH_ANY,
            ),
        ],
        rules=LDAPClient.MATCH_ALL,
    )
    attrs = ["cn", "ipaConfigString"]
    if not dirsrv.is_running():
        raise IpactlError(
            "Failed to get list of services to probe status:\n"
            "Directory Server is stopped",
            3,
        )

    try:
        # The start/restart functions already wait for the server to be
        # started. What we are doing with this wait is really checking to see
        # if the server is listening at all.
        lurl = ldapurl.LDAPUrl(api.env.ldap_uri)
        if lurl.urlscheme == "ldapi":
            wait_for_open_socket(
                lurl.hostport, timeout=api.env.startup_timeout
            )
        else:
            (host, port) = lurl.hostport.split(":")
            wait_for_open_ports(
                host, [int(port)], timeout=api.env.startup_timeout
            )
        con = LDAPClient(api.env.ldap_uri)
        con.external_bind()
        res = con.get_entries(
            base,
            filter=srcfilter,
            attrs_list=attrs,
            scope=con.SCOPE_SUBTREE,
            time_limit=10,
        )
    except errors.NetworkError:
        # LSB status code 3: program is not running
        raise IpactlError(
            "Failed to get list of services to probe status:\n"
            "Directory Server is stopped",
            3,
        )
    except errors.NotFound:
        masters_list = []
        dn = DN(
            ("cn", "masters"), ("cn", "ipa"), ("cn", "etc"), api.env.basedn
        )
        attrs = ["cn"]
        try:
            entries = con.get_entries(
                dn, con.SCOPE_ONELEVEL, attrs_list=attrs
            )
        except Exception as e:
            masters_list.append(
                "No master found because of error: %s" % str(e)
            )
        else:
            for master_entry in entries:
                masters_list.append(master_entry.single_value["cn"])

        masters = "\n".join(masters_list)

        raise IpactlError(
            "Failed to get list of services to probe status!\n"
            "Configured hostname '%s' does not match any master server in "
            "LDAP:\n%s"
            % (api.env.host, masters)
        )
    except Exception as e:
        raise IpactlError(
            "Unknown error when retrieving list of services from LDAP: %s"
            % str(e)
        )

    svc_list = []

    for entry in res:
        name = entry.single_value["cn"]
        for p in entry["ipaConfigString"]:
            if p.startswith("startOrder "):
                try:
                    order = int(p.split()[1])
                except ValueError:
                    raise IpactlError(
                        "Expected order as integer in: %s:%s" % (name, p)
                    )
        svc_list.append([order, name])

    ordered_list = []
    for order, svc in sorted(svc_list):
        if svc in service.SERVICE_LIST:
            ordered_list.append(service.SERVICE_LIST[svc].systemd_name)
    return deduplicate(ordered_list)


def get_config_from_file(rval):
    """
    Get the list of configured services from the cached file.

    :param rval: The return value for any exception that is raised.
    """

    svc_list = []

    try:
        f = open(tasks.get_svc_list_file(), "r")
        svc_list = json.load(f)
    except Exception as e:
        raise IpactlError(
            "Unknown error when retrieving list of services from file: %s"
            % str(e),
            4
        )

    # the framework can start/stop a number of related services we are not
    # authoritative for, so filter the list through SERVICES_LIST and order it
    # accordingly too.

    def_svc_list = []
    for svc in service.SERVICE_LIST:
        s = service.SERVICE_LIST[svc]
        def_svc_list.append([s[1], s[0]])

    ordered_list = []
    for _order, svc in sorted(def_svc_list):
        if svc in svc_list:
            ordered_list.append(svc)

    return deduplicate(ordered_list)


def stop_services(svc_list):
    for svc in svc_list:
        svc_off = services.service(svc, api=api)
        try:
            svc_off.stop(capture_output=False)
        except Exception:
            pass


def stop_dirsrv(dirsrv):
    try:
        dirsrv.stop(capture_output=False)
    except Exception:
        pass


def ipa_start(options):

    if not options.skip_version_check:
        version_check()
    else:
        print("Skipping version check")

    if os.path.isfile(tasks.get_svc_list_file()):
        emit_err("Existing service file detected!")
        emit_err("Assuming stale, cleaning and proceeding")
        # remove file with list of started services
        # This is ok as systemd will just skip services
        # that are already running and just return, so that the
        # stop() method of the base class will simply fill in the
        # service file again
        os.unlink(paths.SVC_LIST_FILE)

    dirsrv = services.knownservices.dirsrv
    try:
        print("Starting Directory Service")
        dirsrv.start(
            capture_output=get_capture_output("dirsrv", options.debug)
        )
    except Exception as e:
        raise IpactlError("Failed to start Directory Service: " + str(e))

    try:
        svc_list = get_config(dirsrv)
    except Exception as e:
        emit_err("Failed to read data from service file: " + str(e))
        emit_err("Shutting down")

        if not options.ignore_service_failures:
            stop_dirsrv(dirsrv)

        if isinstance(e, IpactlError):
            # do not display any other error message
            raise IpactlError(rval=e.rval)
        else:
            raise IpactlError()

    if len(svc_list) == 0:
        # no service to start
        return

    for svc in svc_list:
        svchandle = services.service(svc, api=api)
        try:
            print("Starting %s Service" % svc)
            svchandle.start(
                capture_output=get_capture_output(svc, options.debug)
            )
        except Exception:
            emit_err("Failed to start %s Service" % svc)
            # if ignore_service_failures is specified, skip rollback and
            # continue with the next service
            if options.ignore_service_failures:
                emit_err(
                    "Forced start, ignoring %s Service, "
                    "continuing normal operation"
                    % svc
                )
                continue

            emit_err("Shutting down")
            stop_services(svc_list)
            stop_dirsrv(dirsrv)

            emit_err(MSG_HINT_IGNORE_SERVICE_FAILURE)
            raise IpactlError("Aborting ipactl")


def ipa_stop(options):
    dirsrv = services.knownservices.dirsrv
    try:
        svc_list = get_config_from_file(rval=4)
    except Exception as e:
        # Issue reading the file ? Let's try to get data from LDAP as a
        # fallback
        try:
            dirsrv.start(capture_output=False)
            svc_list = get_config(dirsrv)
        except Exception as e:
            emit_err("Failed to read data from Directory Service: " + str(e))
            emit_err("Shutting down")
            try:
                # just try to stop it, do not read a result
                dirsrv.stop()
            finally:
                raise IpactlError()

    for svc in reversed(svc_list):
        svchandle = services.service(svc, api=api)
        try:
            print("Stopping %s Service" % svc)
            svchandle.stop(capture_output=False)
        except Exception:
            emit_err("Failed to stop %s Service" % svc)

    try:
        print("Stopping Directory Service")
        dirsrv.stop(capture_output=False)
    except Exception:
        raise IpactlError("Failed to stop Directory Service")

    # remove file with list of started services
    try:
        os.unlink(paths.SVC_LIST_FILE)
    except OSError:
        pass


def ipa_restart(options):
    if not options.skip_version_check:
        try:
            version_check()
        except Exception as e:
            try:
                ipa_stop(options)
            except Exception:
                # We don't care about errors that happened while stopping.
                # We need to raise the upgrade error.
                pass
            raise e
    else:
        print("Skipping version check")

    dirsrv = services.knownservices.dirsrv
    new_svc_list = []
    dirsrv_restart = True
    if not dirsrv.is_running():
        try:
            print("Starting Directory Service")
            dirsrv.start(
                capture_output=get_capture_output("dirsrv", options.debug)
            )
            dirsrv_restart = False
        except Exception as e:
            raise IpactlError("Failed to start Directory Service: " + str(e))

    try:
        new_svc_list = get_config(dirsrv)
    except Exception as e:
        emit_err("Failed to read data from Directory Service: " + str(e))
        emit_err("Shutting down")
        try:
            dirsrv.stop(capture_output=False)
        except Exception:
            pass
        if isinstance(e, IpactlError):
            # do not display any other error message
            raise IpactlError(rval=e.rval)
        else:
            raise IpactlError()

    old_svc_list = []
    try:
        old_svc_list = get_config_from_file(rval=4)
    except Exception as e:
        emit_err("Failed to get service list from file: " + str(e))
        # fallback to what's in LDAP
        old_svc_list = new_svc_list

    # match service to start/stop
    svc_list = []
    for s in new_svc_list:
        if s in old_svc_list:
            svc_list.append(s)

    # remove commons
    for s in svc_list:
        if s in old_svc_list:
            old_svc_list.remove(s)
    for s in svc_list:
        if s in new_svc_list:
            new_svc_list.remove(s)

    if len(old_svc_list) != 0:
        # we need to definitely stop some services
        for svc in reversed(old_svc_list):
            svchandle = services.service(svc, api=api)
            try:
                print("Stopping %s Service" % svc)
                svchandle.stop(capture_output=False)
            except Exception:
                emit_err("Failed to stop %s Service" % svc)

    try:
        if dirsrv_restart:
            print("Restarting Directory Service")
            dirsrv.restart(
                capture_output=get_capture_output("dirsrv", options.debug)
            )
    except Exception as e:
        emit_err("Failed to restart Directory Service: " + str(e))
        emit_err("Shutting down")

        if not options.ignore_service_failures:
            stop_services(reversed(svc_list))
            stop_dirsrv(dirsrv)

        raise IpactlError("Aborting ipactl")

    if len(svc_list) != 0:
        # there are services to restart
        for svc in svc_list:
            svchandle = services.service(svc, api=api)
            try:
                print("Restarting %s Service" % svc)
                svchandle.restart(
                    capture_output=get_capture_output(svc, options.debug)
                )
            except Exception:
                emit_err("Failed to restart %s Service" % svc)
                # if ignore_service_failures is specified,
                # skip rollback and continue with the next service
                if options.ignore_service_failures:
                    emit_err(
                        "Forced restart, ignoring %s Service, "
                        "continuing normal operation"
                        % svc
                    )
                    continue

                emit_err("Shutting down")
                stop_services(svc_list)
                stop_dirsrv(dirsrv)

                emit_err(MSG_HINT_IGNORE_SERVICE_FAILURE)
                raise IpactlError("Aborting ipactl")

    if len(new_svc_list) != 0:
        # we still need to start some services
        for svc in new_svc_list:
            svchandle = services.service(svc, api=api)
            try:
                print("Starting %s Service" % svc)
                svchandle.start(
                    capture_output=get_capture_output(svc, options.debug)
                )
            except Exception:
                emit_err("Failed to start %s Service" % svc)
                # if ignore_service_failures is specified, skip rollback and
                # continue with the next service
                if options.ignore_service_failures:
                    emit_err(
                        "Forced start, ignoring %s Service, "
                        "continuing normal operation"
                        % svc
                    )
                    continue

                emit_err("Shutting down")
                stop_services(svc_list)
                stop_dirsrv(dirsrv)

                emit_err(MSG_HINT_IGNORE_SERVICE_FAILURE)
                raise IpactlError("Aborting ipactl")


def ipa_status(options):
    """Report status of IPA-owned processes

       The LSB defines the possible status values as:

       0 program is running or service is OK
       1 program is dead and /var/run pid file exists
       2 program is dead and /var/lock lock file exists
       3 program is not running
       4 program or service status is unknown
       5-99 reserved for future LSB use
       100-149 reserved for distribution use
       150-199 reserved for application use
       200-254 reserved

       We only really care about 0, 3 and 4.
    """
    socket_activated = ('ipa-ods-exporter', 'ipa-otpd',)

    try:
        dirsrv = services.knownservices.dirsrv
        if dirsrv.is_running():
            svc_list = get_config(dirsrv)
        else:
            svc_list = get_config_from_file(rval=1)
    except IpactlError as e:
        if os.path.exists(tasks.get_svc_list_file()):
            raise e
        else:
            svc_list = []
    except Exception as e:
        raise IpactlError(
            "Failed to get list of services to probe status: " + str(e),
            4
        )

    stopped = 0
    dirsrv = services.knownservices.dirsrv
    try:
        if dirsrv.is_running():
            print("Directory Service: RUNNING")
        else:
            print("Directory Service: STOPPED")
            stopped = 1
    except Exception as e:
        raise IpactlError("Failed to get Directory Service status", 4)

    if len(svc_list) == 0:
        raise IpactlError(
            (
                "Directory Service must be running in order to "
                "obtain status of other services"
            ),
            3,
        )

    for svc in svc_list:
        svchandle = services.service(svc, api=api)
        try:
            if svchandle.is_running():
                print("%s Service: RUNNING" % svc)
            else:
                print("%s Service: STOPPED" % svc)
                if svc not in socket_activated:
                    stopped += 1
        except Exception:
            emit_err("Failed to get %s Service status" % svc)

    if stopped > 0:
        raise IpactlError("%d service(s) are not running" % stopped, 3)


def main():
    if not os.getegid() == 0:
        # LSB status code 4: user had insufficient privilege
        raise IpactlError("You must be root to run ipactl.", 4)

    _safe_options, options, args = parse_options()

    if len(args) != 1:
        # LSB status code 2: invalid or excess argument(s)
        raise IpactlError("You must specify one action", 2)
    elif args[0] not in ("start", "stop", "restart", "status"):
        raise IpactlError("Unrecognized action [" + args[0] + "]", 2)

    # check if IPA is configured at all
    try:
        check_IPA_configuration()
    except IpactlError as e:
        if args[0].lower() == "status":
            # Different LSB return code for status command:
            # 4 - program or service status is unknown
            # This should differentiate uninstalled IPA from status
            # code 3 - program is not running
            e.rval = 4
            raise e
        else:
            raise e

    api.bootstrap(
        in_server=True,
        context="ipactl",
        confdir=paths.ETC_IPA,
        debug=options.debug,
    )
    api.finalize()

    if "." not in api.env.host:
        raise IpactlError(
            "Invalid hostname '%s' in IPA configuration!\n"
            "The hostname must be fully-qualified" % api.env.host
        )

    if args[0].lower() == "start":
        ipa_start(options)
    elif args[0].lower() == "stop":
        ipa_stop(options)
    elif args[0].lower() == "restart":
        ipa_restart(options)
    elif args[0].lower() == "status":
        ipa_status(options)
