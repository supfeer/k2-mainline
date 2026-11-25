# Support for Automatic belt tensioning module
#
# Copyright 
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import numpy as np
from .box_wrapper import BoxAction

class COM:
    def __init__(self):
        self.head = 0xf7     # Frame header
        self.addr = 0x21     # Device address
        self.len  = 3        # Payload byte length
        self.star = 0x00	 # Status byte
        self.fun  = 0x00     # Function byte
        self.data = []       # Payload bytes
        self.crc  = 0xff     # CRC byte
        pass

class STA:
    def __init__(self):
        self.read_version_cmd = 0      # Command: read module firmware version
        self.read_version_resp = 0     # Reply length: read firmware version
        self.read_flash_cmd = 2        # Command: read flash calibration variables
        self.read_flash_resp = 2       # Reply length: read flash variables
        self.write_flash_cmd = 4       # Command: write flash calibration variables
        self.write_flash_resp = 4      # Reply length: write flash variables
        self.read_adc_cmd = 6          # Command: read current strain gauge ADC
        self.read_adc_resp = 6         # Reply length: read strain gauge ADC
        self.move_slider_cmd = 8       # Command: move slider relative distance
        self.move_slider_resp = 8      # Reply length: move slider
        pass


def split_to_bytes(data):   # Split one 32-bit integer into 4 big-endian bytes
    # Mask input to unsigned 32-bit integer
    data = data & 0xFFFFFFFF
    return [((data >> (8 * i)) & 0xFF) for i in range(4)][::-1]

def bytes_to_int(byte_array):    # Combine 4 big-endian bytes into one 32-bit value
    # Require 4 input bytes
    assert len(byte_array) == 4
    result = 0
    for i, byte in enumerate(byte_array):
        # Shift and OR each byte into place
        result |= (byte & 0xFF) << (8 * (3 - i))
    # Return assembled 32-bit integer
    return result    




class MDL:
    def __init__(self, val):
        self.value = val             # Instance index
        # self.name = None           # Sensor name
        self.softversion = 0         # Sensor firmware version
        self.halversion = 0          # Sensor hardware version
        self.current_status = 0      # Module status bitmask
        self.total_place = 0         # Maximum slider travel (um)
        self.total_tension = 0       # Strain gauge full scale (0.01 N)
        self.idl_adc = 0xffffffff            # Calibration ADC reading at load 1
        self.full_adc = 0xffffffff           # Calibration ADC reading at load 2
        self.current_place_adc = 0xffffffff  # Current strain gauge ADC value
        self.current_place = 0x0000          # Current slider position (um)
        self.current_tension = 0             # Current tension/compression force (N)
        self.target_place = 0                # Target slider position
        self.target_move = 0             # Pending slider travel distance
        self.target_tension = 0          # Target belt tension force
        self.target_error = 0            # Allowed tension tolerance band
        self.error_result = 0            # Measured tension result
        self.tension_correction_dir = 0  # Calibration direction
        self.uart_pin = None             # Serial transport handle
        self.convert = False             # Telemetry refresh flag
        self.read_flash = False          # Flash readback flag
        self.slope = 0                   # Linear fit slope
        self.intercept = 0               # Linear fit intercept
        self.mistake = 0.02              # Auto-tensioning tolerance band
        self.adjustnum1 = 140            # Calibration reference force 1
        self.adjustnum2 = 160            # Calibration reference force 2
        pass

class BELT_MDL:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")

        name_parts = config.get_name().split()
        self.base_name = name_parts[0]
        self.name = name_parts[-1]

        self.mdl = {}
        self.mdl = MDL(self)
        self.com = {}
        self.com = COM()
        self.sta = {}
        self.sta = STA()
        
        self.mdl.total_place = config.getint('ch_max_dis', default=5, minval=0, maxval=10)
        self.mdl.target_tension = config.getint('ch_best_n', default=140, minval=0, maxval=300)
        self.mdl.target_error = config.getfloat('ch_best_error', default=0.05, minval=0.001, maxval=1)

        self._serial = self.printer.lookup_object("serial_485 " + "serial485")
        
        
        self.gcode.register_mux_command("BELT_MDL_INFO", "MDL_NAME", self.name,            # Query all calibration and status registers of the module
                                   self.cmd_BELT_MDL_INFO,desc=self.cmd_BELT_MDL_INFO_help)
        self.gcode.register_mux_command("BELT_MDL_MOVE", "MDL_NAME", self.name,            # Move module slider to requested position
                                   self.cmd_BELT_MDL_MOVE,desc=self.cmd_BELT_MDL_MOVE_help)
        self.gcode.register_mux_command("BELT_MDL_SET", "MDL_NAME", self.name,             # Tension belt until target force is reached
                                   self.cmd_BELT_MDL_SET,desc=self.cmd_BELT_MDL_SET_help)
        self.gcode.register_mux_command("BELT_MDL_CALI", "MDL_NAME", self.name,            # Calibrate travel stroke and strain gauge limits without belt installed
                                   self.cmd_BELT_MDL_CALI,desc=self.cmd_BELT_MDL_CALI_help)
        self.gcode.register_mux_command("BELT_MDL_TEST", "MDL_NAME", self.name,            # Verify belt tension falls within target tolerance band
                                   self.cmd_BELT_MDL_TEST,desc=self.cmd_BELT_MDL_TEST_help)
        
        self.webhooks = self.printer.lookup_object('webhooks')
        self.webhooks.register_mux_endpoint("belt_mdl_test", "MDL_NAME", self.name,
                                 self._belt_mdl_test)
        pass


    def get_mdl_data(self):   # Verify current belt tension matches target band
        self.config_addr()   
        self.mdl_info_cmd()
        self.error_result = 1
        target_max = self.mdl.target_tension*(1+self.mdl.target_error)
        target_min = self.mdl.target_tension*(1-self.mdl.target_error)
        if((self.mdl.current_tension>target_min)&(self.mdl.current_tension<target_max)):
            self.error_result = 0
        self.gcode.respond_info("MDL_NAME: %s"%(self.name,))
        result = {'current_tension': self.mdl.current_tension, 'target_error': self.error_result}
        return result

    cmd_BELT_MDL_TEST_help = "BELT_MDL_TEST."
    def cmd_BELT_MDL_TEST(self, gcmd):
        self.gcode.respond_info("get_mdl_data: %s" % (self.get_mdl_data()))
        pass

    def _belt_mdl_test(self, web_request):
        web_request.send(self.get_mdl_data())
        pass


    def mdl_info_cmd(self):
        self.gcode.respond_info("ACK_mdl_info")
        self.Get_version()
        self.get_flash_data()
        self.init_adc_to_num()
        self.get_adc()
        self.adc_to_num(self.mdl.current_place_adc)

        # reactor = self.printer.get_reactor()
        # self.gcode.respond_info("reactor: %s"%(reactor,))
        # self.gcode.respond_info("reactor1: %s"%(reactor.monotonic(),))
        # reactor.pause(reactor.monotonic() + 10.0)
        # self.gcode.respond_info("reactor2: %s"%(reactor,))
        # self.gcode.respond_info("reactor3: %s"%(reactor.monotonic(),))
        # self.get_flash_data()
        # self.init_adc_to_num()
        # self.get_adc()
        # self.adc_to_num(self.mdl.current_place_adc)
        # self.mdl_info.send([self.oid])
        # self.Get_version()
        # self.get_flash_data()
        # self.get_adc()
        # self.set_move(1,0x11223344)
        # self.write_flash()
        # mdl_info_buf = self.mdl_info.send([self.oid])
        # self.gcode.respond_info("ACK_mdl_info: %s"%str(mdl_info_buf))
        pass

    def set_place(self):         # Move slider toward target position
        self.gcode.respond_info("ACK_mdl_pos")
        if((self.mdl.current_place==0xffffffff)):
            if self.name == 'mdlx':
                raise self.printer.command_error("""{"code":"key714", "msg":"Belt tension module strain gauge not calibrated abnormal: '%s' set_place_error", "values": []}"""% (self.name))
            if self.name == 'mdly':
                raise self.printer.command_error("""{"code":"key715", "msg":"Belt tension module strain gauge not calibrated abnormal: '%s' set_place_error", "values": []}"""% (self.name))
        self.target_move = self.target_place - self.mdl.current_place
        self.mdl.current_place = self.target_place
        movedir = 1 if self.target_move>0 else 0
        movenum = abs(self.target_move)
        self.set_move(movedir,movenum)
        self.write_flash()
        # mdl_pos_buf = self.mdl_pos.send([self.oid, 0])
        # self.gcode.respond_info("ACK_mdl_pos: %s"%str(mdl_pos_buf))
        pass


    def run_tension(self):    
        movetimes = 0
        while(1):
            movetimes += 1 
            self.gcode.respond_info("times:%s"%movetimes)
            if(movetimes > 200):                                      # Abort after 200 iterations if leadscrew nonlinearity prevents convergence
                return 0
            aimpull = self.target_tension - self.mdl.current_tension   # Signed force delta relative to target
            aimpull = abs(aimpull)                                     # Unsigned magnitude of error
            aimpull = 10 if aimpull > 10 else aimpull                  # Clamp control step
            aimmove = int(aimpull) + 2                                 # Adaptive step scheduling for coarse-to-fine convergence
            if(aimmove > movetimes):                                   # Attenuate step size with iteration count to damp oscillation
                aimmove = aimmove - movetimes                          # Reduce step size after initial coarse attempts
            elif(movetimes > 110):                                      # Switch to minimum step after 80 unsuccessful iterations
                aimmove = 1                                            # Fine-step adjustment against mechanical backlash
            elif(movetimes > 70):                                      # Finer step after 50 iterations
                aimmove = 2                                            # Step size trade-off between precision and settling time
            elif(movetimes > 50):                                      # Intermediate step reduction after 30 iterations
                aimmove = 3                                            # Ensure cumulative span covers full backlash window
            elif(movetimes > 40):                                      # Coarse step reduction after 20 iterations
                aimmove = 4                                            # Small-step adjustment against mechanical backlash
            elif(movetimes > 20):                                      # Extra linear-decay pass for convergence margin
                if(aimmove > (movetimes-20)):                          
                    aimmove = aimmove + 20 - movetimes 
            if(self.target_tension*(1+self.mdl.mistake) <  self.mdl.current_tension):
                # self.set_move(0,aimmove)
                # self.mdl.current_place = self.mdl.current_place - aimmove
                self.get_adc()
                self.adc_to_num(self.mdl.current_place_adc)
                return 1
            elif(self.target_tension*(1-self.mdl.mistake) >  self.mdl.current_tension):
                self.set_move(1,aimmove)
                self.mdl.current_place = self.mdl.current_place + aimmove
                self.get_adc()
                self.adc_to_num(self.mdl.current_place_adc)
            else:
                return 1    
    
    def set_tension(self):
        self.init_adc_to_num()
        self.get_adc()
        self.adc_to_num(self.mdl.current_place_adc)
        for i in range(2):
            self.run_tension()
            self.gcode.run_script_from_command("RESET_HOME_AXES_XY")
            self.gcode.run_script_from_command("MOTOR_CONTROL NUM=1 DATA=2")  
            self.gcode.run_script_from_command("MOTOR_CONTROL NUM=2 DATA=2")
            self.printer.get_reactor().pause(self.printer.get_reactor().monotonic() + 0.3)
            self.gcode.run_script_from_command("FORCE_MOVE STEPPER=stepper_x DISTANCE=-200 VELOCITY=60")
            self.gcode.run_script_from_command("FORCE_MOVE STEPPER=stepper_y DISTANCE=200 VELOCITY=60")
            self.gcode.run_script_from_command("M400")
            self.gcode.run_script_from_command("FORCE_MOVE STEPPER=stepper_x DISTANCE=200 VELOCITY=60")
            self.gcode.run_script_from_command("FORCE_MOVE STEPPER=stepper_y DISTANCE=-200 VELOCITY=60")
            self.gcode.run_script_from_command("M400")
            self.gcode.run_script_from_command("MOTOR_CONTROL NUM=1 DATA=1")
            self.gcode.run_script_from_command("MOTOR_CONTROL NUM=2 DATA=1")
            self.gcode.run_script_from_command("M84")
        run_result = self.run_tension()
        if(run_result == 0):
            if self.name == 'mdlx':
                raise self.printer.command_error("""{"code":"key718", "msg":"Belt tensioning timeout: '%s'", "values": []}"""% (self.name))
            if self.name == 'mdly':
                raise self.printer.command_error("""{"code":"key719", "msg":"Belt tensioning timeout: '%s'", "values": []}"""% (self.name))
        self.write_flash()
        pass

    def set_tension_correction(self):
        if(self.tension_correction_dir==0):
            self.mdl.current_place = 0 
            self.write_flash()
        elif(self.tension_correction_dir==1):
            self.mdl.adjustnum1 = self.tension_correction_ten
            self.get_adc()
            self.mdl.idl_adc = self.mdl.current_place_adc
            self.write_flash()
        elif(self.tension_correction_dir==2):
            self.mdl.adjustnum2 = self.tension_correction_ten
            self.get_adc()
            self.mdl.full_adc = self.mdl.current_place_adc
            self.write_flash()
        
            # self.get_flash_data()
        
        # mdl_init_buf = self.mdl_init.send([self.oid, self.tension_correction_dir])
        # self.gcode.respond_info("ACK_mdl_init: %s"%str(mdl_init_buf))
        pass

    cmd_BELT_MDL_INFO_help = "obtain detailed parameters for the given module"
    def cmd_BELT_MDL_INFO(self, gcmd):  
        self.config_addr()   
        self.mdl_info_cmd()
        self.gcode.respond_info("MDL_NAME: %s"%(self.name,))
        pass
    
    cmd_BELT_MDL_MOVE_help = "set the position of the module slider."
    def cmd_BELT_MDL_MOVE(self, gcmd):
        self.config_addr() 
        self.target_place= gcmd.get_int('POS', 0)
        self.set_place()
        self.gcode.respond_info("MDL_NAME: %s"%(self.name,))
        pass

    cmd_BELT_MDL_SET_help = "set the module tension value."
    def cmd_BELT_MDL_SET(self, gcmd):
        self.config_addr() 
        self.target_tension= gcmd.get_int('MDL_N', 140.0)
        self.set_tension()
        self.gcode.respond_info("MDL_NAME: %s"%(self.name,))
        pass

    cmd_BELT_MDL_CALI_help = "calibrate stroke and pressure."
    def cmd_BELT_MDL_CALI(self, gcmd):
        self.config_addr() 
        self.tension_correction_dir= gcmd.get_int('DIR', 0)
        self.tension_correction_ten= gcmd.get_int('TEN', None)
        self.set_tension_correction()
        self.gcode.respond_info("MDL_NAME: %s"%(self.name,))
        pass

    def Get_version(self):                   # Read hardware and software versions
        sendbuf = self.send_sensor_data(self.sta.read_version_cmd,[])
        # self.gcode.respond_info("sendbuf: %s"%sendbuf)
        uartbuf = self.send_data(sendbuf) 
        # self.gcode.respond_info("resetbuf: %s"%uartbuf)
        redata = self.recv_sensor_data(uartbuf)
        if(redata[0] == 0):
            self.gcode.respond_info("reset:start error")
            return redata
        if(redata[1] != self.sta.read_version_resp):   # Verify response function code
            self.gcode.respond_info("reset:comfun error")
            redata = 0,-5,0
            return redata
        if(redata[1] == self.sta.read_version_resp):   # Function code matched
            self.mdl.halversion  = redata[2][:2]    # Hardware version string
            self.mdl.softversion = redata[2][2:]    # Software version string
            self.gcode.respond_info("halversion:%s"%self.mdl.halversion)
            self.gcode.respond_info("softversion:%s"%self.mdl.softversion)
        pass

    def Get_flash(self,flash_num):                   # Read flash calibration parameters
        sendbuf = self.send_sensor_data(self.sta.read_flash_cmd,[flash_num])
        # self.gcode.respond_info("sendbuf: %s"%sendbuf)
        uartbuf = self.send_data(sendbuf)
        # self.gcode.respond_info("resetbuf: %s"%uartbuf)
        redata = self.recv_sensor_data(uartbuf)
        if(redata[0] == 0):
            self.gcode.respond_info("reset:start error")
            return redata
        if(redata[1] != self.sta.read_flash_resp):   # Verify response function code
            self.gcode.respond_info("reset:comfun error")
            redata = 0,-5,0
            return redata
        if(redata[1] == self.sta.read_flash_resp):   # Function code matched
            # print("redata",redata)
            return redata
        pass

    def get_flash_data(self):                 
        flash_buf = self.Get_flash(3)
        # self.gcode.respond_info("flash_data:%s"%flash_buf[2])
        if(flash_buf[0] == 0):   # Invalid register data
            return None
        if(flash_buf[0] == 1):
            flash_data_num = flash_buf[2][0]  # Extract payload length
            flash_data0 = flash_buf[2][1:5]   # Slice payload into 4-byte 32-bit fields
            flash_data1 = flash_buf[2][5:9]
            flash_data2 = flash_buf[2][9:13]
            flash_data_num0 = bytes_to_int(flash_data0)   # Decode 4-byte tuple into 32-bit integer
            flash_data_num1 = bytes_to_int(flash_data1)
            flash_data_num2 = bytes_to_int(flash_data2)
            self.mdl.current_place = flash_data_num0       # Current slider position (0.01 mm)
            self.mdl.idl_adc = flash_data_num1             # Unloaded strain gauge ADC tare value
            self.mdl.full_adc = flash_data_num2            # Full-scale strain gauge ADC value
            self.gcode.respond_info("current_place:%s"%self.mdl.current_place)
            self.gcode.respond_info("idl_adc:%s"%self.mdl.idl_adc)
            self.gcode.respond_info("full_adc:%s"%self.mdl.full_adc)
            if((self.mdl.current_place==0xffffffff)|(self.mdl.idl_adc==0xffffffff)|(self.mdl.full_adc==0xffffffff)):
                if self.name == 'mdlx':
                    raise self.printer.command_error("""{"code":"key714", "msg":"Belt tension module strain gauge not calibrated abnormal: '%s'", "values": []}"""% (self.name))
                if self.name == 'mdly':
                    raise self.printer.command_error("""{"code":"key715", "msg":"Belt tension module strain gauge not calibrated abnormal: '%s'", "values": []}"""% (self.name))
        pass

    def write_flash_buf(self,flash_num,flash_data):   # Write flash calibration record (count + fields)
        flash_data_buf = [flash_num]
        flash_data_buf.extend(flash_data)
        sendbuf = self.send_sensor_data(self.sta.write_flash_cmd,flash_data_buf)
        # self.gcode.respond_info("sendbuf: %s"%sendbuf)
        uartbuf = self.send_data(sendbuf)
        # self.gcode.respond_info("resetbuf: %s"%uartbuf)
        redata = self.recv_sensor_data(uartbuf)
        if(redata[0] == 0):
            self.gcode.respond_info("reset:start error")
            return redata
        if(redata[1] != self.sta.write_flash_resp):    # Verify response function code
            self.gcode.respond_info("reset:comfun error")
            redata = 0,-5,0
            return redata
        if(redata[1] == self.sta.write_flash_resp):    # Function code matched
            # print("redata",redata)
            return redata
        pass   

    def write_flash(self):               # Pack flash calibration fields
        data0 = self.mdl.current_place       # Current slider position (0.01 mm)
        data1 = self.mdl.idl_adc             # Unloaded strain gauge ADC tare value
        data2 = self.mdl.full_adc            # Full-scale strain gauge ADC value
        buf_data0 = split_to_bytes(data0)   # Encode 32-bit integer into 4 bytes
        buf_data1 = split_to_bytes(data1)   
        buf_data2 = split_to_bytes(data2)
        data_buf = buf_data0                # Append encoded bytes
        data_buf.extend(buf_data1)
        data_buf.extend(buf_data2)
        flash_buf = self.write_flash_buf(3,data_buf)   # Send 3 calibration words
        self.gcode.respond_info("flash_data:%s"%flash_buf[2])
        if(flash_buf[0] == 0):    # Verify response frame
            return None
        if(flash_buf[0] == 1):
            flash_data_num = flash_buf[2][0]   # Payload byte length
            flash_data0 = flash_buf[2][1:5]    # Payload bytes
            flash_data1 = flash_buf[2][5:9]
            flash_data2 = flash_buf[2][9:13]
            flash_data_num0 = bytes_to_int(flash_data0)     # Decode echoed 32-bit calibration value
            flash_data_num1 = bytes_to_int(flash_data1)
            flash_data_num2 = bytes_to_int(flash_data2)
            if((self.mdl.current_place == flash_data_num0)|(self.mdl.idl_adc == flash_data_num1)|(self.mdl.full_adc == flash_data_num2)): 
                return 1     # Flash write verified
            else:
                return -1    # Flash write verification mismatch
        pass 

    def get_adc_buf(self):                       # Read strain gauge ADC value
        sendbuf = self.send_sensor_data(self.sta.read_adc_cmd,[])         
        # self.gcode.respond_info("sendbuf: %s"%sendbuf)
        uartbuf = self.send_data(sendbuf)
        # self.gcode.respond_info("resetbuf: %s"%uartbuf)
        redata = self.recv_sensor_data(uartbuf)
        if(redata[0] == 0):
            self.gcode.respond_info("reset:start error")
            return redata
        if(redata[1] != self.sta.read_adc_resp):     # Verify response function code
            self.gcode.respond_info("reset:comfun error")
            redata = 0,-5,0
            return redata
        if(redata[1] == self.sta.read_adc_resp):     # Function code matched
            # self.gcode.respond_info("redata:%s"%redata[2])
            return redata
        pass 

    def get_adc(self):                       # Parse 32-bit ADC sample
        adc_buf = self.get_adc_buf()
        self.gcode.respond_info("adc_buf:%s"%adc_buf[2])
        if(adc_buf[0] == 0):   # Verify response frame
            return None 
        if(adc_buf[0]==1):
            adc_num = bytes_to_int(adc_buf[2])
            self.mdl.current_place_adc = adc_num   # Latest strain gauge ADC reading
            self.gcode.respond_info("adc_num:%s"%adc_num)
            return adc_num
        pass 

    def set_move_buf(self,dir,rang_buf):                   # Move slider by signed displacement
        set_move_buf = [dir]
        set_move_buf.extend(rang_buf)
        sendbuf = self.send_sensor_data(self.sta.move_slider_cmd,set_move_buf)
        # self.gcode.respond_info("sendbuf: %s"%sendbuf)
        uartbuf = self.send_data(sendbuf)
        # self.gcode.respond_info("resetbuf: %s"%uartbuf)
        redata = self.recv_sensor_data(uartbuf)
        if(redata[0] == 0):
            self.gcode.respond_info("reset:start error")
            return redata
        if(redata[1] != self.sta.move_slider_resp):     # Verify response function code
            self.gcode.respond_info("reset:comfun error")
            redata = 0,-5,0 
            return redata
        if(redata[1] == self.sta.move_slider_resp):
            # print("redata",redata)
            return redata
        pass 
    
    def set_move(self,dir,rang_num):           # Encode signed displacement
        if((rang_num >= 0)&(rang_num < 0xffff)):
            rang_buf = []
            rang_buf = split_to_bytes(rang_num)    # Split 32-bit value into 4 bytes
            move_buf = self.set_move_buf(dir,rang_buf)
            delaytime = rang_num * 0.0132
            # delaytime = int(delaytime)
            reactor = self.printer.get_reactor()
            reactor.pause(reactor.monotonic() + delaytime)
            self.gcode.respond_info("move_buf:%s"%move_buf[2])
            if(move_buf[0] == 0):   # Verify response frame
                return None 
            if(move_buf[0]==1):
                move_num_buf = move_buf[2][1:]
                move_num = bytes_to_int(move_num_buf)
                self.gcode.respond_info("move_num:%s"%move_num)
                self.gcode.respond_info("current_place:%s"%self.mdl.current_place)
                return move_num
        else:
            self.gcode.respond_info("move_num error:%s"%move_num)
        pass 

    def init_adc_to_num(self):    # Fit linear ADC-to-force calibration curve
        if((self.mdl.current_place==0xffffffff)|(self.mdl.idl_adc==0xffffffff)|(self.mdl.full_adc==0xffffffff)):
            if self.name == 'mdlx':
                raise self.printer.command_error("""{"code":"key714", "msg":"Belt tension module strain gauge not calibrated abnormal: '%s'adc_to_num_error", "values": []}"""% (self.name))
            if self.name == 'mdly':
                raise self.printer.command_error("""{"code":"key715", "msg":"Belt tension module strain gauge not calibrated abnormal: '%s'adc_to_num_error", "values": []}"""% (self.name))
        if((self.mdl.idl_adc==0x00000000)|(self.mdl.full_adc==0x00000000)):
            if self.name == 'mdlx':
                raise self.printer.command_error("""{"code":"key720", "msg":"The calibration value of the strain gauge is zero: '%s'calibration_num_error", "values": []}"""% (self.name))
            if self.name == 'mdly':
                raise self.printer.command_error("""{"code":"key721", "msg":"The calibration value of the strain gauge is zero: '%s'calibration_num_error", "values": []}"""% (self.name))
        # adc_difference = abs(self.mdl.idl_adc-self.mdl.full_adc)
        # if(adc_difference<30000):
        #     if self.name == 'mdlx':
        #         raise self.printer.command_error("""{"code":"key720", "msg":"The calibration value of the strain gauge is too small: '%s'calibration_num_error", "values": []}"""% (self.name))
        #     if self.name == 'mdly':
        #         raise self.printer.command_error("""{"code":"key721", "msg":"The calibration value of the strain gauge is too small: '%s'calibration_num_error", "values": []}"""% (self.name))
        xd = [self.mdl.idl_adc,self.mdl.full_adc]
        yd = [self.mdl.adjustnum1,self.mdl.adjustnum2]
        xn = np.array(xd)
        yn = np.array(yd)
        # Compute first-degree linear fit using numpy.polyfit
        # Degree 1 linear regression
        coefficients = np.polyfit(xn, yn, 1)
        slope, intercept = coefficients
        self.mdl.slope = slope
        self.mdl.intercept = intercept
        self.gcode.respond_info("slope:%s"%slope)
        self.gcode.respond_info("intercept:%s"%intercept)
        pass

    def adc_to_num(self,adc_data):     # Convert ADC counts to belt tension (N)
        if((self.mdl.slope == 0)&(self.mdl.intercept == 0)):
            return None   #error ADC parameters must be initialized first
        adc_num = adc_data * self.mdl.slope + self.mdl.intercept
        self.mdl.current_tension = adc_num
        self.gcode.respond_info("pull_num:%s"%adc_num)
        if((adc_num>600)|(adc_num<-300)):     # Reject out-of-range force readings outside [-300, 600]
            if self.name == 'mdlx':
                raise self.printer.command_error("""{"code":"key716", "msg":"Abnormal belt tension: '%s' pull_num_error", "values": []}"""% (self.name))
            if self.name == 'mdly':
                raise self.printer.command_error("""{"code":"key717", "msg":"Abnormal belt tension: '%s' pull_num_error", "values": []}"""% (self.name))
        return adc_num
        pass
    
    def config_addr(self):
        self.com.addr = 0x22 if self.name == 'mdly' else 0x21
        pass

        

    def send_sensor_data(self,sen_fun,sen_data):   # Build RS-485 request packet from function code and payload
        # com = COM()
        self.com.len  = len(sen_data)+3
        self.com.data = sen_data
        self.com.fun  = sen_fun
        self.com.crc  = 0
        combuf = []
        combuf.append(self.com.head)    # Header byte
        combuf.append(self.com.addr)    # Slave address
        combuf.append(self.com.len)     # Payload byte length
        combuf.append(self.com.star)    # Status byte
        combuf.append(self.com.fun)     # Function byte
        combuf.extend(self.com.data)    # Data bytes
        combuf.append(self.com.crc)     # Append CRC-8
        return combuf
        pass

    def recv_sensor_data(self,rec_data):    # Decode RS-485 response packet
        # com = COM()
        if(rec_data == None):          # Check for empty or short response
            self.gcode.respond_info("reset:resetbuf is None")
            if self.name == 'mdlx':
                raise self.printer.command_error("""{"code":"key712", "msg":"Communication abnormality of belt automatic tensioning module 485: '%s'", "values": []}"""% (self.name))
            if self.name == 'mdly':
                raise self.printer.command_error("""{"code":"key713", "msg":"Communication abnormality of belt automatic tensioning module 485: '%s'", "values": []}"""% (self.name))
            # Raise error on missing RS-485 response
            return 0,-1,0
        if(len(rec_data)<3):           # Validate packet byte length
            self.gcode.respond_info("reset: <3")
            return 0,-2,0
        if(rec_data[0] != self.com.head):   # Validate header byte
            self.gcode.respond_info("reset: head error")
            return 0,-3,0
        if(rec_data[1] != self.com.addr):   # Validate slave address
            self.gcode.respond_info("reset: head error")
            return 0,-4,0
        leng = rec_data[2]             # Extract payload length
        # print("leng",leng)
        sen_fun = rec_data[4]          # Extract function code
        sen_data = rec_data[5:leng-3+5]  # Extract payload bytes
        return 1,sen_fun,sen_data      # Return (ok_flag, status_or_func, payload)
        pass

    def send_data(self,hex_data):
        hexsendbuf = hex_data[1:-1]
        readbuf = self._serial.cmd_send_data_with_response(hexsendbuf, 1)
        return readbuf

    def get_status(self, eventime):
        return dict(tension=self.mdl.current_tension)

def load_config(config):      # Config loader for single [belt_mdl] section
    prt = BELT_MDL(config)
    # config.get_printer().add_object('probe', probe.PrinterProbe(config, prt))
    return prt

def load_config_prefix(config):    # Config loader for named [belt_mdl <name>] section
    prt = BELT_MDL(config)
    # config.get_printer().add_object('probe', probe.PrinterProbe(config, prt))
    return prt




 










