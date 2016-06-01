# -*- coding: utf-8 -*-
import http
import time
import util
import exceptions
import compat
import verlib
import re
from xml.etree import ElementTree

import signalsmixin
import plexobjects
import plexresource
import plexlibrary
import plexapp
# from plexapi.client import Client
# from plexapi.playqueue import PlayQueue


TOTAL_QUERIES = 0
DEFAULT_BASEURI = 'http://localhost:32400'


class Hub(plexobjects.PlexObject):
    def init(self, data):
        self.items = []
        for elem in data:
            try:
                self.items.append(plexobjects.buildItem(self.server, elem, '/hubs'))
            except exceptions.UnknownType:
                print 'Unkown hub item type({1}): {0}'.format(elem, elem.attrib.get('type'))

    def __repr__(self):
        return '<{0}:{1}>'.format(self.__class__.__name__, self.hubIdentifier)


class PlexServer(plexresource.PlexResource, signalsmixin.SignalsMixin):
    def __init__(self, data=None):
        signalsmixin.SignalsMixin.__init__(self)
        plexresource.PlexResource.__init__(self, data)
        self.accessToken = None
        self.multiuser = False
        self.isSupported = None
        self.hasFallback = False
        self.supportsAudioTranscoding = False
        self.supportsVideoTranscoding = False
        self.supportsPhotoTranscoding = False
        self.supportsVideoRemuxOnly = False
        self.supportsScrobble = True
        self.allowsMediaDeletion = False
        self.allowChannelAccess = False
        self.activeConnection = None
        self.serverClass = None

        self.pendingReachabilityRequests = 0
        self.pendingSecureRequests = 0

        self.features = {}
        self.librariesByUuid = {}

        self.server = self
        self.session = http.Session()

        self.owner = None
        self.owned = False
        self.synced = False
        self.sameNetwork = False
        self.uuid = None
        self.name = None
        self.platform = None
        self.versionNorm = None

        if data is None:
            return

        self.owner = data.attrib.get('sourceTitle')
        self.owned = data.attrib.get('owned') == '1'
        self.synced = data.attrib.get('synced') == '1'
        self.sameNetwork = data.attrib.get('publicAddressMatches') == '1'
        self.uuid = data.attrib.get('clientIdentifier')
        self.name = data.attrib.get('name')
        self.platform = data.attrib.get('platform')
        self.versionNorm = verlib.suggest_normalized_version(data.attrib.get('productVersion'))

    def __eq__(self, other):
        if not other:
            return False
        if self.__class__ != other.__class__:

            return False
        return self.uuid == other.uuid and self.owner == other.owner

    def __ne__(self, other):
        return not self.__eq__(other)

    def __str__(self):
        return "<PlexServer {0} owned: {1} uuid: {2}>".format(self.name, self.owned, self.uuid)

    def __repr__(self):
        return self.__str__()

    @property
    def isSecure(self):
        if self.activeConnection:
            return self.activeConnection.isSecure

    def hubs(self, section=None, count=None):
        hubs = []

        q = '/hubs'
        params = {}
        if section:
            q = '/hubs/sections/%s' % section

        if count is not None:
            params = {'count': count}

        for elem in self.query(q, params=params):
            hubs.append(Hub(elem, server=self))
        return hubs

    @property
    def library(self):
        util.LOG(repr(self.__dict__))
        if self.platform == 'cloudsync':
            return plexlibrary.Library(None, server=self)
        else:
            return plexlibrary.Library(self.query('/library/'), server=self)

    def buildUrl(self, path, includeToken=False):
        if self.activeConnection:
            return self.activeConnection.buildUrl(self, path, includeToken)
        else:
            util.WARN_LOG("Server connection is None, returning an empty url")
            return ""

    def query(self, path, method=None, **kwargs):
        method = method or self.session.get
        url = self.buildUrl(path, includeToken=True)
        util.LOG('{0} {1}'.format(method.__name__.upper(), url))
        response = method(url, **kwargs)
        if response.status_code not in (200, 201):
            codename = http.codes.get(response.status_code, ['Unknown'])[0]
            raise exceptions.BadRequest('({0}) {1}'.format(response.status_code, codename))
        data = response.text.encode('utf8')

        return ElementTree.fromstring(data) if data else None

    def getImageTranscodeURL(self, path, width, height, extraOpts=None):
        # Build up our parameters
        params = "&width={0}&height={1}".format(width, height)

        if extraOpts is not None:
            for key in extraOpts:
                params += "&{0}={1}".format(key, extraOpts[key])

        if "://" in path:
            imageUrl = self.convertUrlToLoopBack(path)
        else:
            imageUrl = "http://127.0.0.1:" + self.getLocalServerPort() + path

        path = "/photo/:/transcode?url=" + compat.quote(imageUrl) + params

        # Try to use a better server to transcode for synced servers
        if self.synced:
            import plexservermanager
            selectedServer = plexservermanager.MANAGER.getTranscodeServer("photo")
            if selectedServer:
                return selectedServer.buildUrl(path, True)

        return self.buildUrl(path, True)

    def isReachable(self, onlySupported=True):
        if onlySupported and not self.isSupported:
            return False

        return self.activeConnection and self.activeConnection.state == plexresource.ResourceConnection.STATE_REACHABLE

    def isLocalConnection(self):
        return self.activeConnection and (self.sameNetwork or self.activeConnection.isLocal)

    def isRequestToServer(self, url):
        if not self.activeconnection:
            return False

        schemeAndHost = ''.join(self.baseuri.split(':', 2)[0:2])

        return url[:len(schemeAndHost)] == schemeAndHost

    def getToken(self):
        # It's dangerous to use for each here, because it may reset the index
        # on self.connections when something else was in the middle of an iteration.

        for i in range(len(self.connections)):
            conn = self.connections[i]
            if conn.token:
                return conn.token

        return None

    def getLocalServerPort(self):
        # TODO(schuyler): The correct thing to do here is to iterate over local
        # connections and pull out the port. For now, we're always returning 32400.

        return '32400'

    def collectDataFromRoot(self, data):
        print '.    {0}'.format(self.name)
        # Make sure we're processing data for our server, and not some other
        # server that happened to be at the same IP.
        if self.uuid != data.attrib.get('machineIdentifier'):
            util.LOG("Got a reachability response, but from a different server")
            return False

        self.serverClass = data.attrib.get('serverClass')
        self.supportsAudioTranscoding = data.attrib.get('transcoderAudio') == '1'
        self.supportsVideoTranscoding = data.attrib.get('transcoderVideo') == '1' or data.attrib.get('transcoderVideoQualities')
        self.supportsVideoRemuxOnly = data.attrib.get('transcoderVideoRemuxOnly') == '1'
        self.supportsPhotoTranscoding = data.attrib.get('transcoderPhoto') == '1' or (
            not data.attrib.get('transcoderPhoto') and not self.synced and not self.isSecondary()
        )
        self.allowChannelAccess = data.attrib.get('allowChannelAccess') == '1' or (
            not data.attrib.get('allowChannelAccess') and self.owned and not self.synced and not self.IsSecondary()
        )
        self.supportsScrobble = not self.isSecondary() or self.synced
        self.allowsMediaDeletion = not self.synced and self.owned and data.attrib.get('allowMediaDeletion') == '1'
        self.multiuser = data.attrib.get('multiuser') == '1'
        self.name = data.attrib.get('friendlyName') or self.name
        self.platform = data.attrib.get('platform')

        # TODO(schuyler): Process transcoder qualities

        if data.attrib.get('version'):
            self.versionNorm = verlib.suggest_normalized_version('.'.join(data.attrib.get('version', '').split('.', 4)[:4]))

        if verlib.suggest_normalized_version('0.9.11.11') <= self.versionNorm:
            self.features["mkv_transcode"] = True

        if verlib.suggest_normalized_version('0.9.12.5') <= self.versionNorm:
            self.features["allPartsStreamSelection"] = True

        appMinVer = plexapp.INTERFACE.getGlobal('minServerVersionArr', '0.0.0.0')
        self.isSupported = self.isSecondary() or verlib.suggest_normalized_version(appMinVer) <= self.versionNorm

        util.DEBUG_LOG("Server information updated from reachability check: {0}".format(self))

        return True

    def updateReachability(self, force=True, allowFallback=False):
        if not force and self.activeConnection and self.activeConnection.state != plexresource.ResourceConnection.STATE_UNKNOWN:
            return

        util.LOG('Updating reachability for {0}: connections={1}, allowFallback={2}'.format(self.name, len(self.connections), allowFallback))

        epoch = time.time()
        retrySeconds = 60
        minSeconds = 10
        for i in range(len(self.connections)):
            conn = self.connections[i]
            diff = epoch - conn.lastTestedAt or 0
            if conn.hasPendingRequest:
                util.DEBUG_LOG("Skipping reachability test for {0} (has pending request)".format(conn))
            elif diff < minSeconds or (not self.isSecondary() and self.isReachable() and diff < retrySeconds):
                util.DEBUG_LOG("Skipping reachability test for {0} (checked {1} seconds ago)".format(conn, diff))
            elif conn.testReachability(self, allowFallback):
                print repr(self.pendingReachabilityRequests)
                self.pendingReachabilityRequests += 1
                if conn.isSecure:
                    self.pendingSecureRequests += 1

                if self.pendingReachabilityRequests == 1:
                    self.trigger("started:reachability")

        if self.pendingReachabilityRequests <= 0:
            self.trigger("completed:reachability")

    def cancelReachability(self):
        for i in range(len(self.connections)):
            conn = self.connections[i]
            conn.cancelReachability()

    def onReachabilityResult(self, connection):
        connection.lastTestedAt = time.time()
        connection.hasPendingRequest = None
        self.pendingReachabilityRequests -= 1
        if connection.isSecure:
            self.pendingSecureRequests -= 1

        util.DEBUG_LOG("Reachability result for {0}: {1} is {2}".format(self.name, connection.address, connection.state))

        # Noneate active connection if the state is unreachable
        if self.activeConnection and self.activeConnection.state != plexresource.ResourceConnection.STATE_REACHABLE:
            self.activeConnection = None

        # Pick a best connection. If we already had an active connection and
        # it's still reachable, stick with it. (replace with local if
        # available)
        best = self.activeConnection
        for i in range(len(self.connections) - 1, -1, -1):
            conn = self.connections[i]

            if not best or conn.getScore() > best.getScore():
                best = conn

        if best and best.state == best.STATE_REACHABLE:
            if best.isSecure or self.pendingSecureRequests <= 0:
                self.activeConnection = best
            else:
                util.DEBUG_LOG("Found a good connection for {0}, but holding out for better".format(self.name))

        if self.pendingReachabilityRequests <= 0:
            # Retest the server with fallback enabled. hasFallback will only
            # be True if there are available insecure connections and fallback
            # is allowed.

            if self.hasFallback:
                self.updateReachability(False, True)
            else:
                self.trigger("completed:reachability")

        util.LOG("Active connection for {0} is {1}".format(self.name, self.activeConnection))

        import plexservermanager
        plexservermanager.MANAGER.updateReachabilityResult(self, bool(self.activeConnection))

    def markAsRefreshing(self):
        for i in range(len(self.connections)):
            conn = self.connections[i]
            conn.refreshed = False

    def markUpdateFinished(self, source):
        # Any connections for the given source which haven't been refreshed should
        # be removed. Since removing from a list is hard, we'll make a new list.
        toKeep = []
        hasSecureConn = False

        for i in range(len(self.connections)):
            conn = self.connections[i]
            if not conn.refreshed:
                conn.sources = conn.sources and not source

                # If we lost our plex.tv connection, don't remember the token.
                if source == conn.SOURCE_MYPLEX:
                    conn.token = None

            if conn.sources:
                if conn.address[:5] == "https":
                    hasSecureConn = True
                toKeep.append(conn)
            else:
                util.DEBUG_LOG("Removed connection for {0} after updating connections for {1}".format(self.name, source))
                if conn == self.activeConnection:
                    util.DEBUG_LOG("Active connection lost")
                    self.activeConnection = None

        # Update fallback flag if our connections have changed
        if len(toKeep) != len(self.connections):
            for conn in toKeep:
                conn.isFallback = hasSecureConn and conn.address[:5] != "https"

        self.connections = toKeep

        return len(self.connections) > 0

    def merge(self, other):
        # Wherever this other server came from, assume its information is better
        # except for manual connections.

        if other.sourceType != plexresource.ResourceConnection.SOURCE_MANUAL:
            self.name = other.name
            self.versionNorm = other.versionNorm
            self.sameNetwork = other.sameNetwork

        # Merge connections
        for otherConn in other.connections:
            merged = False
            for i in range(len(self.connections)):
                myConn = self.connections[i]
                if myConn == otherConn:
                    myConn.merge(otherConn)
                    merged = True
                    break

            if not merged:
                self.connections.append(otherConn)

        next

        # If the other server has a token, then it came from plex.tv, which
        # means that its ownership information is better than ours. But if
        # it was discovered, then it may incorrectly claim to be owned, so
        # we stick with whatever we already had.

        if other.getToken():
            self.owned = other.owned
            self.owner = other.owner

    def supportsFeature(self, feature):
        return feature in self.features

    def getVersion(self):
        if not self.versionNorm:
            return ''

        return str(self.versionNorm)

    def convertUrlToLoopBack(self, url):
        # If the URL starts with our server URL, replace it with 127.0.0.1:32400.
        if self.isRequestToServer(url):
            url = "http://127.0.0.1:32400" + url[len(self.baseuri) - 1:]

        return url

    def resetLastTest(self):
        for i in range(len(self.connections)):
            conn = self.connections[i]
            conn.lastTestedAt = None

    def isSecondary(self):
        return self.serverClass == "secondary"

    def getLibrarySectionByUuid(self, uuid=None):
        if not uuid:
            return None
        return self.librariesByUuid[uuid]

    def setLibrarySectionByUuid(self, uuid, library):
        self.librariesByUuid[uuid] = library

    def hasInsecureConnections(self):
        if plexapp.INTERFACE.getPreference('allow_insecure') == 'always':
            return False

        # True if we have any insecure connections we have disallowed
        for i in range(len(self.connections)):
            conn = self.connections[i]
            if not conn.isSecure and conn.state == conn.STATE_INSECURE:
                return True

        return False

    def hasSecureConnections(self):
        for i in range(len(self.connections)):
            conn = self.connections[i]
            if conn.isSecure:
                return True

        return False

    def getLibrarySectionPrefs(self, uuid):
        # TODO: Make sure I did this right - ruuk
        librarySection = self.getLibrarySectionByUuid(uuid)

        if librarySection and librarySection.key:
            # Query and store the prefs only when asked for. We could just return the
            # items, but it'll be more useful to store the pref ids in an associative
            # array for ease of selecting the pref we need.

            if not librarySection.sectionPrefs:
                path = "/library/sections/{0}/prefs".format(librarySection.key)
                data = self.query(path)
                if data:
                    librarySection.sectionPrefs = {}
                    for elem in data:
                        item = plexobjects.buildItem(self, elem, path)
                        if item.id:
                            librarySection.sectionPrefs[item.id] = item

            return librarySection.sectionPrefs

        return None

    def swizzleUrl(self, url, includeToken=False):
        m = re.Search("^\w+:\/\/.+?(\/.+)", url)
        newUrl = m and m.group(1) or None
        return self.buildUrl(newUrl or url, includeToken)


class PlexServerOld(plexresource.PlexResource):
    def init(self, data):
        plexresource.PlexResource.init(self, data)
        self.server = self
        self.session = http.Session()

    def __repr__(self):
        return '<{0}:{1}>'.format(self.__class__.__name__, self.baseuri)

    def _connect(self):
        try:
            return self.query('/')
        except Exception as err:
            util.LOG('ERROR: {0} - {1}'.format(self.baseuri, err.message))
            raise exceptions.NotFound('No server found at: {0}'.format(self.baseuri))

    def library(self):
        if self.platform == 'cloudsync':
            return plexlibrary.Library(None, server=self)
        else:
            return plexlibrary.Library(self.query('/library/'), server=self)

    def account(self):
        data = self.query('/myplex/account')
        import myplexaccount
        return myplexaccount.MyPlexAccount(self, data)

    # def clients(self):
    #     items = []
    #     for elem in self.query('/clients'):
    #         items.append(Client(self, elem))
    #     return items

    # def client(self, name):
    #     for elem in self.query('/clients'):
    #         if elem.attrib.get('name').lower() == name.lower():
    #             return Client(self, elem)
    #     raise exceptions.NotFound('Unknown client name: %s' % name)

    # def createPlayQueue(self, item):
    #     return PlayQueue.create(self, item)

    def playlists(self):
        return util.listItems(self, '/playlists')

    def playlist(self, title=None):  # noqa
        for item in self.playlists():
            if item.title == title:
                return item
        raise exceptions.NotFound('None playlist title: %s' % title)

    def hubs(self, section=None, count=None):
        hubs = []

        q = '/hubs'
        params = {}
        if section:
            q = '/hubs/sections/%s' % section

        if count is not None:
            params = {'count': count}

        for elem in self.query(q, params=params):
            hubs.append(Hub(elem, server=self))
        return hubs

    def search(self, query, mediatype=None):
        """ Searching within a library section is much more powerful. """
        items = plexobjects.listItems(self, '/search?query=%s' % compat.quote(query))
        if mediatype:
            return [item for item in items if item.type == mediatype]
        return items

    def sessions(self):
        return plexobjects.listItems(self, '/status/sessions')

    def query(self, path, method=None, token=None, **kwargs):
        method = method or self.session.get
        return self.connection.query(path, method, token, **kwargs)

    def url(self, path):
        return self.connection.getUrl(path, self.token)


def dummyPlexServer():
    return createPlexServer()


def createPlexServer():
    return PlexServer()


def createPlexServerForConnection(conn):
    obj = createPlexServer()
    obj.connections.append(conn)
    obj.activeConnection = conn
    return obj


def createPlexServerForName(uuid, name):
    obj = createPlexServer()
    obj.uuid = uuid
    obj.name = name
    return obj


def createPlexServerForResource(resource):
    # resource.__class__ = PlexServer
    # resource.server = resource
    # resource.session = http.Session()
    return resource
