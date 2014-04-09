#
# Copyright 2010 Nick Foster
# 
# This file is part of gr-air-modes
# 
# gr-air-modes is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3, or (at your option)
# any later version.
# 
# gr-air-modes is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with gr-air-modes; see the file COPYING.  If not, write to
# the Free Software Foundation, Inc., 51 Franklin Street,
# Boston, MA 02110-1301, USA.
# 


import time, os, sys, socket
from string import split, join
import air_modes
from datetime import datetime
import time
from air_modes.exceptions import *
import threading
import struct

class dumb_task_runner(threading.Thread):
    def __init__(self, task, interval):
        threading.Thread.__init__(self)
        self._task = task
        self._interval = interval
        self.shutdown = threading.Event()
        self.finished = threading.Event()
        self.setDaemon(True)
        self.start()

    def run(self):
        while not self.shutdown.is_set():
            self._task()
            time.sleep(self._interval)
        self.finished.set()

    def close(self):
        self.shutdown.set()
        self.finished.wait(self._interval)

class output_state:
  def __init__(self, cprdec, port, pub):
    self._s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    self._s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self._s.bind(('', port))
    self._s.listen(1)
    self._s.setblocking(0) #nonblocking
    self._conns = [] #list of active connections
    self._aircraft_id_map = {} # dictionary of icao24 to aircraft IDs
    self._aircraft_id_count = 0 # Current Aircraft ID count

    self._cpr = cprdec

    pub.subscribe("type5_dl", self.output)
    pub.subscribe("type17_dl", self.output)

    #spawn thread to add new connections as they come in
    self._runner = dumb_task_runner(self.add_pending_conns, 0.1)

  def __del__(self):
    self._s.close()

  def get_aircraft_id(self, icao24):
    if icao24 in self._aircraft_id_map:
      return self._aircraft_id_map[icao24]['id']

    # Adding this new ID to the dictionary
    self._aircraft_id_count += 1
    self._aircraft_id_map[icao24] = {}
    self._aircraft_id_map[icao24]['id'] = self._aircraft_id_count

    # Checking to see if we need to clean up in the event that the
    # dictionary is getting too large.
    if len(self._aircraft_id_map) > 1e4:
      minimum = min(self._aircraft_id_map.values()) + (len(self._aircraft_id_map) - 1e4)
      for icao, _vals in self._aircraft_id_map:
        if _vals['id'] < minimum:
            del self._aircraft_id_map[icao]

    # Finally return the new pair
    return self._aircraft_id_count

  def output(self, msg):
    try:
      sbs1_msg = self.parse(msg)
      #print sbs1_msg
      if sbs1_msg is not None:
        for conn in self._conns[:]: #iterate over a copy of the list
          conn.send(sbs1_msg)
    except socket.error:
      self._conns.remove(conn)
      print "Connections: ", len(self._conns)
    except ADSBError:
      pass

  def add_pending_conns(self):
    try:
      conn, addr = self._s.accept()
      self._conns.append(conn)
      print "Connections: ", len(self._conns)
    except socket.error:
      pass

  def current_time(self):
    timenow = datetime.now()
    return [timenow.strftime("%Y/%m/%d"), timenow.strftime("%H:%M:%S.%f")[0:-3]]

  def decode_fs(self, fs):
    if fs == 0:
      return "0,0,0,0"
    elif fs == 1:
      return "0,0,0,1"
    elif fs == 2:
      return "1,0,0,0"
    elif fs == 3:
      return "1,0,0,1"
    elif fs == 4:
      return "1,0,1,"
    elif fs == 5:
      return "0,0,1,"
    else:
      return ",,,"

  def parse(self, msg):
    #assembles a SBS-1-style output string from the received message

    msgtype = msg.data["df"]
    outmsg = None

    if msgtype == 5:
      outmsg = self.pp5(msg.data)
    elif msgtype == 17:
      outmsg = self.pp17(msg.data)
    else:
      raise NoHandlerError(msgtype)
    return outmsg

  def pp5(self, data):
    icao24 = data["aa"]
    aircraft_id = self.get_aircraft_id(icao24)
    self._aircraft_id_map[icao24]['squawk'] = air_modes.decode_id(data["id"])
    self._aircraft_id_map[icao24]['time'] = datetime.now()
    return self.generate_squawk_update(icao24)

  def pp17(self, data):
    icao24 = data["aa"]
    aircraft_id = self.get_aircraft_id(icao24)
    bdsreg = data["me"].get_type()
    #we'll get better timestamps later, hopefully with actual VRT time
    #in them
    [datestr, timestr] = self.current_time()

    if bdsreg == 0x05:
      # Airborne position measurements
      # WRONG (rnge, bearing), is this still true?
      [altitude, decoded_lat, decoded_lon, rnge, bearing] = air_modes.parseBDS05(data, self._cpr)
      if decoded_lat is None: #no unambiguously valid position available
	  return None
      else:
	#print aircraft_id, hex(icao24), altitude, decoded_lat, decoded_lon
	self._aircraft_id_map[icao24]['lat'] = decoded_lat
	self._aircraft_id_map[icao24]['lon'] = decoded_lon
	self._aircraft_id_map[icao24]['alt'] = altitude
	self._aircraft_id_map[icao24]['time'] = datetime.now()
	return self.generate_position_update(icao24)

    elif bdsreg == 0x08:
      (flight_id, typestring) = air_modes.parseBDS08(data)
      self._aircraft_id_map[icao24]['id'] = flight_id
      self._aircraft_id_map[icao24]['time'] = datetime.now()
      return self.generate_id_update(icao24)

    elif bdsreg == 0x09:
      # Airborne velocity measurements
      # WRONG (heading, vert_spd), Is this still true?
      subtype = data["bds09"].get_type()
      if subtype == 0 or subtype == 1:
        parser = air_modes.parseBDS09_0 if subtype == 0 else air_modes.parseBDS09_1
        [velocity, heading, vert_spd] = parser(data)
	#print aircraft_id, hex(icao24), velocity, heading, vert_spd
	self._aircraft_id_map[icao24]['velocity'] = velocity
	self._aircraft_id_map[icao24]['heading'] = heading
	self._aircraft_id_map[icao24]['vert'] = vert_spd
	self._aircraft_id_map[icao24]['time'] = datetime.now()
	return self.generate_velocity_update(icao24)


  def generate_position_update(self, icao24):
    plane = self._aircraft_id_map[icao24]

    # Don't generate message if record is incomplete
    if 'lat' not in self._aircraft_id_map[icao24]:
	return None
    if 'velocity' not in self._aircraft_id_map[icao24]:
	return None

    ts = time.mktime(self._aircraft_id_map[icao24]['time'].timetuple())
    msg = struct.pack("!IIdddd", icao24, 0x1,
				  ts,
				  self._aircraft_id_map[icao24]['lat'],
				  self._aircraft_id_map[icao24]['lon'],
				  self._aircraft_id_map[icao24]['alt'])
    return msg

  def generate_velocity_update(self, icao24):
    plane = self._aircraft_id_map[icao24]

    # Don't generate message if record is incomplete
    if 'lat' not in self._aircraft_id_map[icao24]:
	return None
    if 'velocity' not in self._aircraft_id_map[icao24]:
	return None

    ts = time.mktime(self._aircraft_id_map[icao24]['time'].timetuple())
    msg = struct.pack("!IIdddd", icao24, 0x2,
				  ts,
				  self._aircraft_id_map[icao24]['velocity'],
				  self._aircraft_id_map[icao24]['heading'],
				  self._aircraft_id_map[icao24]['vert'])
    return msg
    


  def generate_squawk_update(self, icao24):
    plane = self._aircraft_id_map[icao24]

    # Don't generate message if record is incomplete
    if 'squawk' not in self._aircraft_id_map[icao24]:
      return None

    ts = time.mktime(self._aircraft_id_map[icao24]['time'].timetuple())
    msg = struct.pack("!IIdH", icao24, 0x3,
			       ts,
			       self._aircraft_id_map[icao24]['squawk'])
    return msg

  def generate_id_update(self, icao24):
    plane = self._aircraft_id_map[icao24]

    # Don't generate message if record is incomplete
    if 'id' not in self._aircraft_id_map[icao24]:
      return None

    ts = time.mktime(self._aircraft_id_map[icao24]['time'].timetuple())
    msg = struct.pack("!IId8s", icao24, 0x4,
			       ts,
			       self._aircraft_id_map[icao24]['id'])
    return msg
    
    























