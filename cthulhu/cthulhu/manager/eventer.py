import datetime
from dateutil import tz
from cthulhu.gevent_util import nosleep
from cthulhu.log import log
from cthulhu.manager.server_monitor import ServiceId
from cthulhu.manager.types import OsdMap, Health
from cthulhu.manager import config

import gevent.event
import gevent.greenlet


# The tick handler is very cheap (no I/O) so we call
# it quite frequently.
TICK_SECONDS = 10

# The time-based checks don't kick in until after
# a grace period, to avoid generating complaints
# about "stale" timestamps immediately after startup
GRACE_PERIOD = 30

# How long must a [server|cluster] be out of contact before
# we generate an event?
CONTACT_THRESHOLD = int(config.get('cthulhu', 'server_contact_threshold'))
CLUSTER_CONTACT_THRESHOLD = int(config.get('cthulhu', 'cluster_contact_threshold'))

CRITICAL = 1
ERROR = 2
WARNING = 3
INFO = 4


def severity_str(severity):
    return {
        CRITICAL: "CRITICAL",
        ERROR: "ERROR",
        WARNING: "WARNING",
        INFO: "INFO"
    }[severity]


def now_utc():
    return datetime.datetime.utcnow().replace(tzinfo=tz.tzutc())


class Eventer(gevent.greenlet.Greenlet):
    """
    I listen to changes from ClusterMonitor and ServerMonitor, and feed
    events into the event log.  I also periodically check some time-based
    conditions in my on_tick method.
    """

    def __init__(self, persister, notifier, server_monitor, clusters):
        super(Eventer, self).__init__()
        self._persister = persister
        self._notifier = notifier
        self._server_monitor = server_monitor
        self._clusters = clusters

        self._complete = gevent.event.Event()

        # Flags for things we have complained about being out of contact
        # with, to avoid generating the same events repeatedly
        self._servers_complained = set()
        self._clusters_complained = set()

    def stop(self):
        log.debug("Eventer stopping")
        self._complete.set()

    def _run(self):
        self._complete.wait(GRACE_PERIOD)
        while not self._complete.is_set():
            self.on_tick()
            self._complete.wait(TICK_SECONDS)
        log.debug("Eventer complete")

    def _emit(self, severity, message, data):
        """
        :param severity: One of the defined serverity values
        :param message: One line human readable string
        :param data: JSON-serializable data to help non-human consumers
                     make sense of the event
        """
        now = now_utc()
        log.info("Eventer._emit: %s/%s/%s" % (now, severity_str(severity), message))
        # TODO Persist

    @nosleep
    def on_tick(self):
        log.debug("Eventer.on_tick")

        now = now_utc()

        for fqdn, server_state in self._server_monitor.servers.items():
            if not server_state.managed:
                # We don't expect messages from unmanaged servers so don't
                # worry about whether they sent us one recently.
                continue

            if now - server_state.last_contact > datetime.timedelta(seconds=CONTACT_THRESHOLD):
                if fqdn not in self._servers_complained:
                    self._emit(WARNING, "Server {fqdn} is late reporting in, last report at {last}".format(
                        fqdn=fqdn, last=server_state.last_contact
                    ), {'fqdn': fqdn})
            else:
                if fqdn in self._servers_complained:
                    self._emit(INFO, "Server {fqdn} regained contact".format(fqdn=fqdn),
                               {'fqdn': fqdn})
                    self._servers_complained.discard(fqdn)

        for fsid, cluster_monitor in self._clusters.items():
            if cluster_monitor.update_time is None or now - cluster_monitor.update_time > datetime.timedelta(
                    seconds=CLUSTER_CONTACT_THRESHOLD):
                if fsid not in self._clusters_complained:
                    self._emit(WARNING, "Cluster '{name}' is late reporting in".format(name=cluster_monitor.name),
                               {'fsid': fsid})
            else:
                if fsid in self._clusters_complained:
                    self._emit(INFO, "Cluster '{name}' regained contact".format(name=cluster_monitor.name),
                               {'fsid': fsid})
                    self._clusters_complained.discard(fsid)

    @nosleep
    def on_sync_object(self, fsid, sync_type, new, old):
        """
        Notification that a newer version of a SyncObject is available, or
        the first version of a SyncObject is available at startup (wherein
        old will be a null SyncObject)

        :param fsid: The FSID of the cluster to which the object belongs
        :param sync_type: A SyncObject subclass
        :param new: A SyncObject
        :param old: A SyncObject (same type as new)
        """
        log.debug("Eventer.on_sync_object: %s" % sync_type.str)

        if old.data is None:
            return

        if sync_type == OsdMap:
            old_osd_ids = set([o['osd'] for o in old.data['osds']])
            new_osd_ids = set([o['osd'] for o in old.data['osds']])
            deleted_osds = old_osd_ids - new_osd_ids
            created_osds = new_osd_ids - old_osd_ids

            def get_fqdn(osd_id):
                return self._server_monitor.get_by_service(ServiceId(fsid, "osd", str(osd_id))).fqdn

            def get_server(osd_id):
                fqdn = get_fqdn(osd_id)
                if fqdn:
                    return " (on %s)" % fqdn
                else:
                    return ""

            # Generate events for removed OSDs
            for osd_id in deleted_osds:
                self._emit(INFO, "OSD {name}.{id}{server} removed from the cluster map".format(
                    name=self._clusters[fsid].name, id=osd_id, server=get_server(osd_id)
                ), {'fsid': fsid, 'id': osd_id, 'fqdn': get_fqdn(osd_id)})

            # Generate events for added OSDs
            for osd_id in created_osds:
                self._emit(INFO, "OSD {name}.{id}{server} added to the cluster map".format(
                    name=self._clusters[fsid].name, id=osd_id, server=get_server(osd_id)
                ), {'fsid': fsid, 'id': osd_id, 'fqdn': get_fqdn(osd_id)})

            # Generate events for changed OSDs
            for osd_id in old_osd_ids & new_osd_ids:
                old_osd = old.osds_by_id[osd_id]
                new_osd = new.osds_by_id[osd_id]
                if old_osd['up'] != new_osd['up']:
                    if bool(new_osd['up']):
                        self._emit(INFO, "OSD {name}.{id} came up{server}".format(
                            name=self._clusters[fsid].name, id=osd_id, server=get_server(osd_id)
                        ), {'fsid': fsid, 'id': osd_id, 'fqdn': get_fqdn(osd_id)})
                    else:
                        self._emit(WARNING, "OSD {name}.{id} went down{server}".format(
                            name=self._clusters[fsid].name, id=osd_id, server=get_server(osd_id)
                        ), {'fsid': fsid, 'id': osd_id, 'fqdn': get_fqdn(osd_id)})

                        # TODO: aggregate OSD notifications by server so that we can say things
                        # like "all the OSDs on server X went down" or "2/3 OSDs on server X went down"
                        # TODO: aggregate OSD notifications by cluster so that we can say "all OSDs
                        # in cluster 'foo' are down"

                        # TODO Generate notifications if all the OSDs on a server are 'down',
                        # the downness OSD map is more recent than the last contact
                        # with the server, and we haven't already reported the server laggy,
                        # to indicate that our best guess here is that the server itself is down.

        if sync_type == Health:
            # Generate notifications for transitions between HEALTH_OK, HEALTH_WARN, HEALTH_ERR
            old_status = old.data['overall_status']
            new_status = new.data['overall_status']
            health_severity = {
                "HEALTH_OK": INFO,
                "HEALTH_WARN": WARNING,
                "HEALTH_ERR": ERROR
            }

            if old_status != new_status:
                if health_severity[new_status] < health_severity[old_status]:
                    # A worsening of health
                    event_sev = health_severity[new_status]
                    msg = "Health of cluster '{name}' degraded from {old} to {new}".format(
                        old=old_status, new=new_status, name=self._clusters[fsid].name)
                else:
                    # An improvement in health
                    event_sev = INFO
                    msg = "Health of cluster '{name}' recovered from {old} to {new}".format(
                        old=old_status, new=new_status, name=self._clusters[fsid].name)

                if health_severity[new_status] < INFO:
                    # XXX I'm not sure how much I like this, it puts data on the screen
                    # which will soon be stale
                    pass
                    # msg += " (%s)" % (new.data['summary'][0]['summary'])
                self._emit(event_sev, msg, {'fsid': fsid})

                # TODO: generate notifications on PG map to indicate anything particularly
                # interesting like things which are in a bad state and won't be recovering
                # TODO: generate events from MDS map
