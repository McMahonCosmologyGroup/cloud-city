#!/usr/bin/env python
# coding: utf-8
from __future__ import unicode_literals
import time
import socket
import os
import argparse
import time
import txaio

ON_RTD = os.environ.get('READTHEDOCS') == 'True'
if not ON_RTD:
    from ocs import ocs_agent, site_config
    from ocs.ocs_twisted import TimeoutLock, Pacemaker


MOXA_DEFAULT_TIMEOUT = 1.0

class MoxaRecirc(object):
    
    LC = bytes.fromhex("ca")
    MSA = bytes.fromhex("00")
    LSA = bytes.fromhex("01")
    
    def __init__(self,port,timeout=MOXA_DEFAULT_TIMEOUT):
        # port is a tuple of form (IP addr, TCP port)
        self.port = port
        self.sock = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        self.sock.setblocking(0)
        self.sock.settimeout(timeout)
        self.sock.connect(self.port)

    def calc_checksum(self, msg):
        """calculate the checksum the recirculator expects"""
        return bytes.fromhex(  format( (sum(msg)%256)^255, '02x') ) 
        
    def _get_temp(self, cmd, cmd_npts):
        msg = self.MSA + self.LSA + cmd + cmd_npts
        msg += self.calc_checksum(msg)
        msg = self.LC+msg

        self.sock.send( msg )
        out = self.sock.recv(1024)
        try:
            temp = int( out[6:-1].hex(),16)*(0.1)
            return temp
        except:
            print(f"Message Parsing Fail, Received:\n{out}")
            return None

    def get_supply_temp(self):
        cmd = bytes.fromhex("20")
        cmd_npts = bytes.fromhex("00")
        return self._get_temp(cmd, cmd_npts)    
    
    def get_return_temp(self):
        cmd = bytes.fromhex("21")
        cmd_npts = bytes.fromhex("00")
        return self._get_temp(cmd, cmd_npts)    
    
    def get_setpoint(self):
        """Get Setpoint from the Recirculator, for now this assumes the
        precision and the units. Also does not check checksum or other extra
        information present in the message.
        """
        cmd = bytes.fromhex("70")
        cmd_npts = bytes.fromhex("00")        
        return self._get_temp(cmd, cmd_npts)

    def set_setpoint(self, sp):
        """Set the setpoint of the recirculator. For now this assuems the
        precision (0.1C) and the units (C).
        """    
        if sp <10 or sp> 50:
            print(f"requested temperature {sp} is outside acceptable range")        
        set_set = bytes.fromhex("F0")
        set_set_ndata = bytes.fromhex("02")
        
        temp =  bytes.fromhex(format( int(sp/0.1), '04x'))
                
        msg = self.MSA + self.LSA + set_set + set_set_ndata + temp
        msg += self.calc_checksum(msg)
        msg = self.LC+msg

        #print(f"Sending message: {msg}")
        self.sock.send( msg )
        out = self.sock.recv(1024)
        try:
            temp = int( out[6:-1].hex(),16)*(0.1)
            return temp
        except:
            print(f"Message Parsing Fail, Received:\n{out}")
            return None


class RecircAgent:
    def __init__(self, agent, ip_addr, port, mode='acq', samp=1):
        """
        Agent for connecting to the moxa box controlling the recirculator
        
        Args: 
            ip_addr: IP address where MOXA server is running
            port: Port the RPi Server is listening on
            mode: 'acq': Start data acquisition on initialize
            samp: default sampling frequency in Hz
        """
        self.ip_addr = ip_addr
        self.port = int(port)
        
        self.recirc = None
        self.initialized = False
        self.take_data = False

        self.agent = agent
        self.log = agent.log
        self.lock = TimeoutLock()
         
        if mode == 'acq':
            self.auto_acq = True
        else:
            self.auto_acq = False
        self.sampling_frequency = float(samp)

        ### register the position feeds
        agg_params = {
            'frame_length' : 10*60, #[sec] 
        }

        self.agent.register_feed('temperatures',
                                 record = True,
                                 agg_params = agg_params,
                                 buffer_time = 0)
    
    def init_recirc_task(self, session, params=None):
        """init_recirc_task(params=None)
        Perform first time setup for communication with Recirculator.

        Args:
            params (dict): Parameters dictionary for passing parameters to
                task.
        """

        if params is None:
            params = {}

        self.log.debug("Trying to acquire lock")
        with self.lock.acquire_timeout(timeout=0, job='init') as acquired:
            # Locking mechanism stops code from proceeding if no lock acquired
            if not acquired:
                self.log.warn("Could not start init because {} is already running".format(self.lock.job))
                return False, "Could not acquire lock."
            # Run the function you want to run
            self.log.debug("Lock Acquired")
            
            self.recirc = MoxaRecirc((self.ip_addr, self.port))
            print("Recirc Connected")
            
        # This part is for the record and to allow future calls to proceed,
        # so does not require the lock
        self.initialized = True
        if self.auto_acq:
            self.agent.start('acq')
        return True, 'Recirc Connected.'

    def start_acq(self, session, params=None):
        """
        params: 
            dict: {'sampling_frequency': float, sampling rate in Hz}

        The most recent positions are stored in the session.data object in the
        format::

            {"temperatures":
                {"setpoint": temperature set point,
                 "supply_temp": temperture of water at output}
            }

        """
        if params is None:
            params = {}

        
        f_sample = params.get('sampling_frequency', self.sampling_frequency)
        pm = Pacemaker(f_sample, quantize=True)

        if not self.initialized or self.recirc is None:
            raise Exception("Connection to Recirculator not initialized")
        
        with self.lock.acquire_timeout(timeout=0, job='acq') as acquired:
            if not acquired:
                self.log.warn("Could not start acq because {} is already running".format(self.lock.job))
                return False, "Could not acquire lock."

            self.log.info(f"Starting Data Acquisition for Recirculator at {f_sample} Hz")
            session.set_status('running')
            self.take_data = True
            last_release = time.time()

            while self.take_data:
                if time.time()-last_release > 1.:
                    if not self.lock.release_and_acquire(timeout=10):
                        self.log.warn(f"Could not re-acquire lock now held by {self.lock.job}.")
                        return False, "could not re-acquire lock"
                    last_release = time.time()
                pm.sleep()

                data = {'timestamp':time.time(), 
                        'block_name':'temperatures','data':{}}

                data['data']['setpoint'] = self.recirc.get_setpoint()
                data['data']['supply_temp'] = self.recirc.get_supply_temp()

                self.agent.publish_to_feed('temperatures',data)
                session.data.update( data['data'] )
        return True, 'Acquisition exited cleanly.'
    
    def stop_acq(self, session, params=None):
        """
        params: 
            dict: {}
        """
        if self.take_data:
            self.take_data = False
            return True, 'requested to stop taking data.'
        else:
            return False, 'acq is not currently running.'

    def set_setpoint_task(self, session, params=None):
        """Move to absolute position relative to stage center (in mm)

        params: {'temperature':float between 10 and 50 (Celcius)}
        """
        if params is None:
            return False, "No Temperature"
        if 'temperature' not in params:
            return False, "No Temperature Given"

        with self.lock.acquire_timeout(timeout=3, job='move') as acquired:
            if not acquired:
                self.log.warn("Could not start move because lock held by" \
                               f"{self.lock.job}")
                return False, "Could not get lock"
            setting = self.recirc.set_setpoint( params.get('temperature') )
            if setting == params.get('temperature'):
                return True, "Temperature Setting Changed"

        return False, "Temp change did not complete correctly?"

def make_parser(parser=None):
    """Build the argument parser for the Agent. Allows sphinx to automatically
    build documentation based on this function.
    """
    if parser is None:
        parser = argparse.ArgumentParser()

    # Add options specific to this agent.
    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--ip-address')
    pgroup.add_argument('--port')
    pgroup.add_argument('--mode')
    pgroup.add_argument('--sampling_frequency')
    return parser


if __name__ == '__main__':
    # For logging
    txaio.use_twisted()
    LOG = txaio.make_logger()

    # Start logging
    txaio.start_logging(level=os.environ.get("LOGLEVEL", "info"))

    parser = make_parser()

    # Interpret options in the context of site_config.
    args = site_config.parse_args(agent_class = 'RecircAgent', parser=parser)
    

    agent, runner = ocs_agent.init_site_agent(args)

    recirc_agent = RecircAgent(agent, args.ip_address, args.port, args.mode, args.sampling_frequency)

    agent.register_task('init_recirc', recirc_agent.init_recirc_task)
    agent.register_process('acq', recirc_agent.start_acq, recirc_agent.stop_acq)
    agent.register_task('set_setpoint', recirc_agent.set_setpoint_task)
    runner.run(agent, auto_reconnect=True)

### Request Current Setpoint
### msg_in = bytes.fromhex("CA 00 01 70 00 8E")
                
### Change Setpoint to 25C
### msg_in = bytes.fromhex("CA 00 01 F0 02 00 FA 12")

#recirc = MoxaRecirc(("192.168.10.20", 4001))

#Asks the Vaccuum controller for... uh... something
#Just replace this text with whatever message you need to send
#test_message = "REQUEST STATUS\r\n"

#Writes message through moxa box port to whatever device you have it connected to. No need to do any weird
#conversion to serial before sending. The moxa box does all the converting for you!

#Reads the response. The moxa will convert it from serial to a bytes string.
#This will stall your kernal if there is no response!!
#response = moxa.readall()
#print(moxa.readall())



