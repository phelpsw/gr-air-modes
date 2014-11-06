#!/usr/bin/env python

from twisted.internet.protocol import Protocol, ReconnectingClientFactory
from twisted.internet import reactor, task
import struct

from datetime import datetime
import calendar
import time

class Echo(Protocol):
    def __init__(self):
        self.buf = ""
        self.targets = {}
        self.timeout = task.LoopingCall(self.cull)
        self.timeout.start(30)

    def dataReceived(self, data):
        self.buf += data
        self.process()

    def process(self):
        while len(self.buf) >= 40:
            (icao, _type, ts, lat, lon, alt) = struct.unpack("!IIdddd", self.buf[:40])
            self.buf = self.buf[40:]

            if _type != 0x1:
                continue

            self.targets[icao] = (ts, lat, lon, alt)

    def get_targets(self):
        return self.targets

    def cull(self):
        """
        Periodically this should be called to dump targets which haven't been
        heard from in a while.
        """
        delete_icaos = []
        for icao, met in self.targets.iteritems():
            print met[0]
            #if (calendar.timegm(datetime.utcnow().utctimetuple()) - met[0]) > 120:
            if (time.time() - met[0]) > 120:
                delete_icaos.append(icao)

        for i in delete_icaos:
            print "deleting", i
            del self.targets[i]


class EchoClientFactory(ReconnectingClientFactory):
    def __init__(self):
        self.connected = False

    def startedConnecting(self, connector):
        print 'Started to connect.'

    def buildProtocol(self, addr):
        print 'Connected.'
        self.connected = True
        self.client = Echo()
        return self.client

    def is_connected(self):
        return self.connected

    def get_client(self):
        return self.client

    def clientConnectionLost(self, connector, reason):
        self.connected = False
        print 'Lost connection.  Reason:', reason
        self.retry(connector)

    def clientConnectionFailed(self, connector, reason):
        self.connected = False
        print 'Connection failed. Reason:', reason
        self.retry(connector)

from twisted.web import server, resource
from lxml import etree
from pykml.factory import KML_ElementMaker as KML

class Simple(resource.Resource):

    def __init__(self, _factory):
        self.factory = _factory

    isLeaf = True
    def render_GET(self, request):
        if not self.factory.is_connected():
            return

        request.responseHeaders.setRawHeaders("content-type", ['application/vnd.google-earth.kml+xml'])

        doc = KML.kml()
        folder = KML.Folder(KML.name("planes"))
        doc.append(folder)
        targ = self.factory.get_client().get_targets()
        for icao, met in targ.iteritems():
            folder.append(
                KML.Placemark(
                    KML.name('%x' % icao),
                    #KML.styleUrl("#pushpin"),
                    KML.Point(
                        KML.extrude(True),
                        KML.altitudeMode('relativeToGround'),
                        KML.coordinates('%f,%f,%f' % (met[2], met[1], met[3])),
                    ),
                ),
            )
        return etree.tostring(etree.ElementTree(doc),pretty_print=True)



if __name__ == "__main__":
    host = "localhost"
    port = 1234
    factory = EchoClientFactory()
    reactor.connectTCP(host, port, factory)

    site = server.Site(Simple(factory))
    reactor.listenTCP(8080, site)

    reactor.run()

