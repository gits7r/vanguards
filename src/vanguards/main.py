import functools
import stem
import time
import sys

import stem.response.events

from . import control
from . import rendguard
from . import vanguards
from . import bandguards
from . import cbtverify

from . import config

from .logger import plog

_MIN_TOR_VERSION_FOR_BW = stem.version.Version("0.3.4.4-rc")

def main():
  try:
    config.apply_config(config._CONFIG_FILE)
  except:
    pass # Default config can be absent.
  options = config.setup_options()

  # If the user specifies a config file, any values there should override
  # any previous config file options, but not options on the command line.
  if options.config_file != config._CONFIG_FILE:
    try:
      config.apply_config(options.config_file)
    except Exception as e:
      plog("ERROR",
           "Specified config file "+options.config_file+\
           " can't be read: "+str(e))
      sys.exit(1)
    options = config.setup_options()

  try:
    # TODO: Use tor's data directory.. or our own
    state = vanguards.VanguardState.read_from_file(config.STATE_FILE)
    plog("INFO", "Current layer2 guards: "+state.layer2_guardset())
    plog("INFO", "Current layer3 guards: "+state.layer3_guardset())
  except Exception as e:
    plog("NOTICE", "Creating new vanguard state file at: "+config.STATE_FILE)
    state = vanguards.VanguardState(config.STATE_FILE)

  stem.response.events.PARSE_NEWCONSENSUS_EVENTS = False

  reconnects = 0
  connected = False
  while options.retry_limit == None or reconnects < options.retry_limit:
    ret = control_loop(state)
    if ret == "closed":
      connected = True
    # Try once per second but only tell the user every 10 seconds
    if ret == "closed" or reconnects % 10 == 0:
      plog("NOTICE", "Tor connection "+ret+". Trying again...")
    reconnects += 1
    time.sleep(1)

  if not connected:
    sys.exit(1)

def control_loop(state):
  try:
    if config.CONTROL_SOCKET != "":
      controller = \
        stem.control.Controller.from_socket_file(config.CONTROL_SOCKET)
    else:
      controller = stem.control.Controller.from_port(config.CONTROL_IP,
                                                     config.CONTROL_PORT)
  except stem.SocketError as e:
    return "failed: "+str(e)

  control.authenticate_any(controller, config.CONTROL_PASS)

  # XXX: Some subset of this should not run every reconnect
  state.new_consensus_event(controller, None)

  if config.ONE_SHOT_VANGUARDS:
    try:
      controller.save_conf()
    except stem.OperationFailed as e:
      plog("NOTICE", "Tor can't save its own config file: "+str(e))
      sys.exit(1)
    plog("NOTICE", "Updated vanguards in torrc. Exiting.")
    sys.exit(0)

  timeouts = cbtverify.TimeoutStats()
  bandwidths = bandguards.BandwidthStats(controller)

  # Thread-safety: state, timeouts, and bandwidths are effectively
  # transferred to the event thread here. They must not be used in
  # our thread anymore.

  if config.ENABLE_RENDGUARD:
    controller.add_event_listener(
                 functools.partial(rendguard.RendGuard.circ_event,
                                   state.rendguard, controller),
                                  stem.control.EventType.CIRC)
  if config.ENABLE_BANDGUARDS:
    controller.add_event_listener(
                 functools.partial(bandguards.BandwidthStats.circ_event, bandwidths),
                                  stem.control.EventType.CIRC)
    controller.add_event_listener(
                 functools.partial(bandguards.BandwidthStats.bw_event, bandwidths),
                                  stem.control.EventType.BW)
    if controller.get_version() >= _MIN_TOR_VERSION_FOR_BW:
      controller.add_event_listener(
                   functools.partial(bandguards.BandwidthStats.circbw_event, bandwidths),
                                    stem.control.EventType.CIRC_BW)
      controller.add_event_listener(
                   functools.partial(bandguards.BandwidthStats.circ_minor_event, bandwidths),
                                    stem.control.EventType.CIRC_MINOR)
    else:
      plog("NOTICE", "In order for bandwidth-based protections to be "+
                      "enabled, you must use Tor 0.3.4.4-rc or newer.")

  if config.ENABLE_CBTVERIFY:
    controller.add_event_listener(
                 functools.partial(cbtverify.TimeoutStats.circ_event, timeouts),
                                  stem.control.EventType.CIRC)
    controller.add_event_listener(
                 functools.partial(cbtverify.TimeoutStats.cbt_event, timeouts),
                                  stem.control.EventType.BUILDTIMEOUT_SET)

  # Thread-safety: We're effectively transferring controller to the event
  # thread here.
  controller.add_event_listener(
               functools.partial(vanguards.VanguardState.new_consensus_event,
                                 state, controller),
                                stem.control.EventType.NEWCONSENSUS)

  # Blah...
  while controller.is_alive():
    time.sleep(1)

  return "closed"
