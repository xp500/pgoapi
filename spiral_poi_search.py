#!/usr/bin/env python
"""
pgoapi - Pokemon Go API
Copyright (c) 2016 tjado <https://github.com/tejado>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
OR OTHER DEALINGS IN THE SOFTWARE.

Author: tjado <https://github.com/tejado>
"""

import os
import re
import sys
import json
import time
import struct
import random
import logging
import requests
import argparse
import pprint
import datetime
import webbrowser
import csv

from pgoapi import PGoApi
from pgoapi.utilities import f2i, h2f
from pgoapi import utilities as util

from google.protobuf.internal import encoder
from geopy.geocoders import GoogleV3
from s2sphere import Cell, CellId, LatLng

import threading
from threading import Thread

log = logging.getLogger(__name__)

def get_pos_by_name(location_name):
    geolocator = GoogleV3()
    loc = geolocator.geocode(location_name)
    if not loc:
        return None

    log.info('Your given location: %s', loc.address.encode('utf-8'))
    log.info('lat/long/alt: %s %s %s', loc.latitude, loc.longitude, loc.altitude)

    return (loc.latitude, loc.longitude, loc.altitude)

def get_cell_ids(lat, long, radius = 10):
    origin = CellId.from_lat_lng(LatLng.from_degrees(lat, long)).parent(15)
    walk = [origin.id()]
    right = origin.next()
    left = origin.prev()

    # Search around provided radius
    for i in range(radius):
        walk.append(right.id())
        walk.append(left.id())
        right = right.next()
        left = left.prev()

    # Return everything
    return sorted(walk)

def encode(cellid):
    output = []
    encoder._VarintEncoder()(output.append, cellid)
    return ''.join(output)

def init_config():
    parser = argparse.ArgumentParser()
    config_file = "config.json"

    # If config file exists, load variables from json
    config = {}
    if os.path.isfile(config_file):
        with open(config_file) as data:
            config.update(json.load(data))
    else:
       print "CONFIG ERROR"
       return None

    for acc in config['accounts']:
      if acc['auth_service'] not in ['ptc', 'google']:
        log.error("Invalid Auth service specified! ('ptc' or 'google')")
        return None

    return config

def main():
    # log settings
    # log format
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(module)10s] [%(levelname)5s] %(message)s')
    # log level for http request class
    logging.getLogger("requests").setLevel(logging.WARNING)
    # log level for main pgoapi class
    logging.getLogger("pgoapi").setLevel(logging.WARNING)
    # log level for internal pgoapi class
    logging.getLogger("rpc_api").setLevel(logging.WARNING)

    config = init_config()
    if not config:
        return

    if config.get('debug'):
        logging.getLogger("requests").setLevel(logging.DEBUG)
        logging.getLogger("pgoapi").setLevel(logging.DEBUG)
        logging.getLogger("rpc_api").setLevel(logging.DEBUG)

    # Load pokemon pokedex to name map
    pokedex = {}
    with open('pokedex.csv', 'r') as csvfile:
     pokedex_reader = csv.reader(csvfile, delimiter=',')
     headers = pokedex_reader.next()
     id_ix = headers.index('species_id')
     name_ix = headers.index('identifier')
     for row in pokedex_reader:
       pokedex[int(row[id_ix])] = row[name_ix] 

    print pokedex

    position = get_pos_by_name(config['location'])
    if not position:
        return
        
    step_size = 0.0015
    step_limit = 3000
    wait_time = 5

    coords = generate_spiral(position[0], position[1], step_size, step_limit)
    print_gmaps_dbug(coords)

    accounts = config['accounts']
    apis = []
    failed_accs = []
    for account in accounts:
      # instantiate pgoapi
      api = PGoApi()

      # provide player position on the earth
      api.set_position(*position)

      login_attempts = 3
      logged_in = False
      while not logged_in and login_attempts > 0:
        logged_in = logged_in - 1
        try:
          if api.login(account['auth_service'], account['username'], account['password']):
            logged_in = True
        except:
          pass

      if not logged_in:
        failed_accs.append(account['username'])
        print account['username'] + ' failed to log in'
        continue

      apis.append(api)

      # chain subrequests (methods) into one RPC call

      # get player profile call
      # ----------------------
      response_dict = api.get_player()
      print('Response dictionary: \n\r{}'.format(pprint.PrettyPrinter(indent=4).pformat(response_dict)))

    if len(failed_accs) > 0:
      print 'The following accounts have failed to log in'
      for p in failed_accs: print p

    if len(apis) == 0:
      print 'All accounts failed to log in, quitting'
      return

    accs = len(apis)

    print 'Running with {} accounts'.format(accs)
    print 'Estimated scan time: {} seconds'.format(1000 * 5 / accs)

    wanted_pokemon = [130, 131, 143, 149]

    threads = []
    for api_ix in range(0, len(apis)):
      coords_for_api = [coords[i] for i in range(api_ix, len(coords), len(apis))]
      thread = Thread(target = find_poi, args = (apis[api_ix], coords_for_api, wait_time, wanted_pokemon, pokedex))
      thread.daemon = True
      threads.append(thread)
      thread.start()
    while threading.active_count() == len(threads) + 1:
      time.sleep(0)

def find_poi(api, coords, wait_time, wanted_pokemon, pokedex):
  while True:
    poi = {'pokemons': {}, 'forts': []}
    i = 0
    for coord in coords:
        print coord
        #i = i + 1
        lat = coord['lat']
        lng = coord['lng']
        api.set_position(lat, lng, 0)

        #get_cellid was buggy -> replaced through get_cell_ids from pokecli
        #timestamp gets computed a different way:
        cell_ids = get_cell_ids(lat, lng)
        timestamps = [0,] * len(cell_ids)

        successful_request = False
        retry_attempts = 3
        while not successful_request and retry_attempts > 0:
          retry_attempts = retry_attempts - 1
          try:
            start_time = time.time()
            response_dict = api.get_map_objects(latitude = util.f2i(lat), longitude = util.f2i(lng), since_timestamp_ms = timestamps, cell_id = cell_ids)
          except:
            response_dict = None
            print("Unexpected error:", sys.exc_info()[0])
          if response_dict:
            if response_dict['responses'] and 'status' in response_dict['responses']['GET_MAP_OBJECTS']:
              if response_dict['responses']['GET_MAP_OBJECTS']['status'] == 1:
                successful_request = True
                for map_cell in response_dict['responses']['GET_MAP_OBJECTS']['map_cells']:
                  if 'wild_pokemons' in map_cell:
                    for pokemon in map_cell['wild_pokemons']:
                      pokekey = get_key_from_pokemon(pokemon)
                      if pokemon['time_till_hidden_ms'] > 0:
                        pokemon['hides_at'] = datetime.datetime.fromtimestamp(time.time() + pokemon['time_till_hidden_ms']/1000).isoformat()
                        if pokemon['pokemon_data']['pokemon_id'] in wanted_pokemon:
                          print 'POKEMON FOUND!'
                          pokemon['name'] = pokedex[pokemon['pokemon_data']['pokemon_id']]
                          print pokemon
                          print_gmaps_dbug([{'lat': pokemon['latitude'], 'lng': pokemon['longitude']}])
                          os.system('say "{} found"'.format(pokemon['name']))
                  #       poi['pokemons'][pokekey] = pokemon
          req_duration = time.time() - start_time
          if req_duration > 0 and req_duration < 5:
            time.sleep(wait_time - req_duration)

        if not successful_request:
          print "Request failed 3 times :("

    # new dict, binary data
    # print('POI dictionary: \n\r{}'.format(json.dumps(poi, indent=2)))
    #print('POI dictionary: \n\r{}'.format(pprint.PrettyPrinter(indent=4).pformat(poi)))
    #print('Open this in a browser to see the path the spiral search took:')
    #print_gmaps_dbug(coords)

def get_key_from_pokemon(pokemon):
    return '{}-{}'.format(pokemon['spawn_point_id'], pokemon['pokemon_data']['pokemon_id'])

def print_gmaps_dbug(coords):
    if len(coords) == 1:
      url_string = 'http://maps.googleapis.com/maps/api/staticmap?size=400x400&markers={},{}'.format(coords[0]['lat'], coords[0]['lng'])
      webbrowser.open(url_string[:-1])
      return
    url_string = 'http://maps.googleapis.com/maps/api/staticmap?size=400x400&path='
    path_coords = []
    last_coord = coords[0]
    if abs(coords[0]['lat'] - coords[1]['lat']) < 0.001:
      dir = 'lng'
    else:
      dir = 'lat' 
    for coord in coords[1:]:
      if abs(coord[dir] - last_coord[dir]) > 0.001:
        path_coords.append(last_coord)
        if dir == 'lng':
          dir = 'lat'
        else:
          dir = 'lng'
      last_coord = coord
      
    #for coord in [coords[i] for i in range(0, len(coords), 25)]:
    for coord in path_coords:
        url_string += '{},{}|'.format(coord['lat'], coord['lng'])
    webbrowser.open(url_string[:-1])

def generate_spiral(starting_lat, starting_lng, step_size, step_limit):
    coords = [{'lat': starting_lat, 'lng': starting_lng}]
    steps,x,y,d,m = 1, 0, 0, 1, 1
    rlow = 0.0
    rhigh = 0.0005

    while steps < step_limit:
        while 2 * x * d < m and steps < step_limit:
            x = x + d
            steps += 1
            lat = x * step_size + starting_lat + random.uniform(rlow, rhigh)
            lng = y * step_size + starting_lng + random.uniform(rlow, rhigh)
            coords.append({'lat': lat, 'lng': lng})
        while 2 * y * d < m and steps < step_limit:
            y = y + d
            steps += 1
            lat = x * step_size + starting_lat + random.uniform(rlow, rhigh)
            lng = y * step_size + starting_lng + random.uniform(rlow, rhigh)
            coords.append({'lat': lat, 'lng': lng})

        d = -1 * d
        m = m + 1
    return coords

if __name__ == '__main__':
    main()
