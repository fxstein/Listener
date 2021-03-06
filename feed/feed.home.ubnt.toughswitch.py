#!/usr/local/bin/python3 -u
"""
    Author:     Oliver Ratzesberger <https://github.com/fxstein>
    Copyright:  Copyright (C) 2016 Oliver Ratzesberger
    License:    Apache License, Version 2.0
"""

# Make sure we have access to SentientHome commons
import os
import sys
try:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)) + '/..')
except:
    exit(1)

# Sentient Home Application
from common.shapp import shApp
from common.sheventhandler import shEventHandler
from common.shutil import boolify

import requests
import json

# Default settings
from cement.utils.misc import init_defaults

defaults = init_defaults('ubnt_toughswitch', 'ubnt_toughswitch')
defaults['ubnt_toughswitch']['poll_interval'] = 5.0


def mapPort(switch, config, port, data):
    event = [{
        'measurement': 'ubnt.toughswitch',
        'tags': {
            'switch': switch,
            'port': port,
            'portname': config['switch.port.%s.name' % port],
            'duplex': boolify(config['switch.port.%s.duplex' % port]),
            'trunk.status': boolify(config['switch.port.%s.trunk.status' %
                                           port]),
            'switch.jumboframes': boolify(config['switch.jumboframes']),
            },
        'fields': {
            }
    }]

    fields = event[0]['fields']

    for key in data.keys():
        if key == 'stats':
            for stat in data[key].keys():
                try:
                    fields[stat] = float(data[key][stat])
                except ValueError:
                    fields[stat] = data[key][stat]
        else:
            fields[key] = data[key]

    return event


with shApp('ubnt_toughswitch', config_defaults=defaults) as app:
    app.run()

    handler = shEventHandler(app, dedupe=True)

    retries = 0
    sessions = []

    addresses = app.config.get('ubnt_toughswitch', 'addr').split(', ')
    switch_port = app.config.get('ubnt_toughswitch', 'port')
    switch_user = app.config.get('ubnt_toughswitch', 'user')
    passwords = app.config.get('ubnt_toughswitch', 'pass').split(', ')
    switch_verify_ssl = app.config.get('ubnt_toughswitch', 'verify_ssl')

    # Setup sessions for each individual switch
    for i in range(len(addresses)):
        sessions.append(requests.session())

    # Since we support multiple switches we require a distinct session to
    # each individual switch to retrieve it config and stats data.
    for i in range(len(addresses)):
        while True:
            try:
                # Prime the session by first retrieving the login page
                r = sessions[i].get(addresses[i] + ':' + switch_port +
                                    '/login.cgi',
                                    verify=(int)(switch_verify_ssl))
                app.log.debug('Response: %s' % r)

                # Now perform login
                r = sessions[i].post(addresses[i] + ':' + switch_port +
                                     '/login.cgi',
                                     params={'username': switch_user,
                                             'password': passwords[i],
                                             'uri': ' /stats'},
                                     verify=(int)(switch_verify_ssl))

                app.log.debug('Response: %s' % r)

                break
            except Exception as e:
                retries += 1

                # Something went wrong authorizing the connection to ubnt ts
                app.log.warn(e)
                app.log.warn('Cannot connect to ToughSwitch. Attemp %s of %s' %
                             (retries, app.retries))

                if retries >= app.retries:
                    app.log.fatal(e)
                    app.log.fatal('Unable to connect to ToughSwitch. Exiting..')
                    app.close(1)

                handler.sleep(app.retry_interval)

    # At this point we have successfully logged into all the switches
    while True:

        # For ever iteration of the feed we request new data from each switch
        # through its dedicated session.
        for i in range(len(addresses)):
            # We might need to retry a switch in case it does not respond
            retries = 0

            while True:
                try:
                    # First we get the most current config data from the switch
                    r = sessions[i].get(addresses[i] + ':' + switch_port +
                                        '/getcfg.cgi',
                                        verify=(int)(switch_verify_ssl))
                    if r.text is not '' and r.text is not None:
                        config = json.loads(r.text)

                        # Next we get the stats data
                        r = sessions[i].get(addresses[i] + ':' + switch_port +
                                            '/stats',
                                            verify=(int)(switch_verify_ssl))
                        if r.text is not '' and r.text is not None:
                            data = json.loads(r.text)
                            break

                    # If we got here it means either config or stats data came
                    # back empty - setup retry
                    handler.sleep(app.retry_interval)

                except Exception as e:
                    retries += 1

                    # Something went wrong connecting to the ubnt mfi service
                    app.log.warn(e)
                    app.log.warn('Cannot connect. Attemp %s of %s' %
                                 (retries, app.retries))

                    if retries >= app.retries:
                        app.log.fatal(e)
                        app.log.fatal('Unable to connect. Exiting...')
                        app.close(1)

                    handler.sleep(app.retry_interval)

            app.log.debug('Config: %s' % json.dumps(config, sort_keys=True))
            app.log.debug('Data: %s' % json.dumps(data, sort_keys=True))

            switch = addresses[i].replace('https://', '')

            # Create a dsitinct event for every port of the switch - even the
            # smaller 5 port switches track 8 ports in their software
            for port in range(1, 9):

                event = mapPort(switch, config, port, data['stats'][str(port)])

                app.log.debug('Event: %s' % event)
                # dedupe automatically ignores events we have processed before
                # This is where the dedupe magic happens. The event handler has
                # deduping built in and keeps an in-memory cache of events of
                # the past ~24h for that. In this case only changed switch data
                # points will get emitted and stored
                handler.postEvent(event, dedupe=True, batch=True)

        # Wait for the next iteration - this is also when batched events get
        # submitted as one array.
        handler.sleep()
