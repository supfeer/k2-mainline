import logging
import copy
from dataclasses import dataclass
from typing import List, Union


########################### Usage Notes ###############################
# Add [auto_addr] section in config
# dev_table_map_table holds MB (CFS), CLM (closed-loop motor), BTM (belt tensioner)
# Comment out unneeded device types from the table
# External callers read addr_manager_table_xx[i].online (1=online, 0=offline)
# Online status updates via periodic online_check (3 consecutive misses -> offline)
# Retries are disabled at transport level and handled at application layer
# Run PRINT_ADDR_TABLE to inspect the active address table
# Updated uniid tables are persisted to config via SAVE_CONFIG
# #*# [auto_addr]
# #*# mb_addr_table_uniids = xxx

# Max frame length
MAX_DATA_LEN = 100  
# Max UID length
MAX_UNIID_LEN = 12
MAX_LOST_CNT = 3
# Frame header
PACK_HEAD = 0xF7
# Broadcast address
BROADCAST_ADDR = 0xFF
BROADCAST_ADDR_MB = 0xFE
BROADCAST_ADDR_CLM = 0xFD
BROADCAST_ADDR_BTM = 0xFC
# Auto-addressing command IDs
CMD_GET_SLAVE_INFO = 0xA1
CMD_SET_SLAVE_ADDR = 0xA0
CMD_ONLINE_CHECK = 0xA2
CMD_GET_ADDR_TABLE = 0xA3
CMD_LOADER_TO_APP = 0x0B

# Device operating state (loader or app)
MODE_APP = 0
MODE_LOADER = 1

# Device types
# MB=material box, CLM=closed-loop motor, BTM=belt tensioner motor
DEV_TYPE_MB = 1
DEV_TYPE_CLM = 2
DEV_TYPE_BTM = 3
# Frame byte offsets
DEV_TYPE_INDEX_OFFSET = DEV_TYPE_MB

# CRC-8 calculation parameters
POLY = 0x07 

# package status
STATUS_OK = 0x00 
# STATUS_ERROR = 1

# online state
ONLINE_STATE_OFFLINE = 0
ONLINE_STATE_ONLINE = 1
ONLINE_STATE_INIT = 2
ONLINE_STATE_WAIT_FOR_ACK = 3

# Max set/get attempts
MAX_GET_TIMES = 2
MAX_SET_TIMES = 2

# Command timeout (s)
TIMEOUT_SHORT_TIME = 0.05
TIMEOUT_MEDIUM_TIME = 0.1
TIMEOUT_LONG_TIME = 1.0 

cmd_timeout = { CMD_GET_SLAVE_INFO: TIMEOUT_LONG_TIME,
                CMD_SET_SLAVE_ADDR: TIMEOUT_SHORT_TIME, 
                CMD_GET_ADDR_TABLE: TIMEOUT_SHORT_TIME,
                CMD_ONLINE_CHECK:   TIMEOUT_MEDIUM_TIME,
                CMD_LOADER_TO_APP:  TIMEOUT_SHORT_TIME }
name_map = {
    DEV_TYPE_MB: "mb_addr_table_uniids",
    # DEV_TYPE_CLM: "clm_addr_table_uniids",
    # DEV_TYPE_BTM: "btm_addr_table_uniids",
}

# Request frame layout
@dataclass
class DataPackage:
    head: int
    slave_addr: int
    length: int
    status: int
    function_code: int
    data: List[Union[int, float]]
    crc: int

# Response payload format
@dataclass
class FcAckData:
    dev_type: int
    mode: int  # 0: app, 1: loader
    uniid: List[int]

@dataclass
class AddrManager:
    addr: int
    uniid: List[int]
    mapped: int
    online: int
    acked: int
    lost_cnt: int
    mode: int

# Material box address slots
addr_manager_table_mb = [
    AddrManager(0x01, [0x00], 0, 0, 0, 0, 0),
    AddrManager(0x02, [0x00], 0, 0, 0, 0, 0),
    AddrManager(0x03, [0x00], 0, 0, 0, 0, 0),
    AddrManager(0x04, [0x00], 0, 0, 0, 0, 0),
]

# Closed-loop motor address slots
addr_manager_table_cl_motor = [
    AddrManager(0x81, [0x00], 0, 0, 0, 0, 0),
    AddrManager(0x82, [0x00], 0, 0, 0, 0, 0),
    AddrManager(0x83, [0x00], 0, 0, 0, 0, 0),
    AddrManager(0x84, [0x00], 0, 0, 0, 0, 0),
]

# Belt tensioner address slots
addr_manager_table_bt_motor = [
    AddrManager(0x91, [0x00], 0, 0, 0, 0, 0),
    AddrManager(0x92, [0x00], 0, 0, 0, 0, 0),
]

class DevTableMap:
    def __init__(self, dev_type, broadcast_addr, addr_manager_table):
        self.dev_type = dev_type
        self.broadcast_addr = broadcast_addr
        self.addr_manager_table = addr_manager_table
        self.size = len(addr_manager_table)

dev_table_map_table = [
    DevTableMap(DEV_TYPE_MB, BROADCAST_ADDR_MB, addr_manager_table_mb),
    # DevTableMap(DEV_TYPE_CLM, BROADCAST_ADDR_CLM, addr_manager_table_cl_motor),
    # DevTableMap(DEV_TYPE_BTM, BROADCAST_ADDR_BTM, addr_manager_table_bt_motor),
]

class AutoAddrWrapper:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.configfile = self.printer.lookup_object('configfile')
        self.config = config
        self.gcode = self.printer.lookup_object('gcode')
        self._serial = self.printer.lookup_object("serial_485 " + "serial485")
        # self.parse = ParseData()
        # self.get_finished = False
        self.debug = config.getint('debug', default=0, minval=0, maxval=1)
        self.uniid_changed = False
        self.print_stats = None
        self.gcode.register_command('PRINT_ADDR_TABLE', self.cmd_PRINT_ADDR_TABLE)

        # Load saved address table from config
        for i in range(len(dev_table_map_table)):
            dev_table_map = dev_table_map_table[i]
            self.get_addr_table_uniids(dev_table_map)

        if config.has_section("motor_control") and config.getsection('motor_control').getint('switch')==1:
            self.printer.register_event_handler('auto_addr:start', self.reg_auto_addr_set)
        else:
            self.printer.register_event_handler('klippy:ready', self.reg_auto_addr_set)

        self.printer.register_event_handler('auto_addr:set_finished', self.reg_auto_addr_process)

        self.dprintf("auto addr wrapper init")

    ###################### Load Saved Address Table ########################
    def get_addr_table_uniids(self, dev_table_map):
        name = name_map[dev_table_map.dev_type]
        if self.config.get(name, None) is not None:
            def custom_int_parser(value):
                try:
                    if value.startswith('0x') or value.startswith('0X'):
                        return int(value, 16)
                    else:
                        return int(value)
                except ValueError as e:
                    raise ValueError(f"Invalid literal for int with base 10 or 16: '{value}'") from e
            uniids = self.config.getlists(name, seps=(',', '\n'), parser=custom_int_parser)
            if len(uniids) != dev_table_map.size:
                self.dprintf("Error: %s, uniids len: %d, dev_table_map.size: %d" % (name, len(uniids), dev_table_map.size))
            else:
                self.dprintf("addr table uniid get %s" % name)
                for i in range(dev_table_map.size):
                    dev_table_map.addr_manager_table[i].uniid = list(uniids[i])
                    ## Non-zero uniid means the slot was assigned on a prior boot
                    if len(uniids[i]) >= 1:
                        if uniids[i][0] != 0:
                            dev_table_map.addr_manager_table[i].mapped = 1
                            dev_table_map.addr_manager_table[i].online = ONLINE_STATE_INIT 
                    self.dprintf("addr %d: %s" % (dev_table_map.addr_manager_table[i].addr, dev_table_map.addr_manager_table[i].uniid))
        else:
            self.dprintf("Error: %s not exist" % name)

    ###################### Save Address Table ########################
    def save_addr_table_uniids(self, dev_table_map):
        dev_type = dev_table_map.dev_type
        name = name_map[dev_type] 
        self.dprintf("addr table uniid save %s" % name)
        uniids = ""
        for i in range(dev_table_map.size):
            uniids += "\n  "
            for j in range(len(dev_table_map.addr_manager_table[i].uniid)):
                uniids += "0x%02X, " % dev_table_map.addr_manager_table[i].uniid[j]
            uniids = uniids[:-2]
        self.dprintf("uniids: %s" % uniids)
        section = "auto_addr"
        self.configfile.set(section, name, uniids)
        self.gcode.run_script_from_command("CXSAVE_CONFIG")

    ###################### gcode cmd ########################
    def cmd_PRINT_ADDR_TABLE(self, gcmd):
        for i in range(len(dev_table_map_table)):
            dev_table_map = dev_table_map_table[i]
            addr_manager_table = dev_table_map.addr_manager_table
            size = len(addr_manager_table)
            for i in range(size):
                response = "0x%02X, " % addr_manager_table[i].addr
                for j in range(len(addr_manager_table[i].uniid)):
                    response += "0x%02X, " % addr_manager_table[i].uniid[j]
                response += "%d, %d, %d, %d, mode:%d" % (addr_manager_table[i].mapped, addr_manager_table[i].online,
                        addr_manager_table[i].acked, addr_manager_table[i].lost_cnt, addr_manager_table[i].mode)
                gcmd.respond_info(response)

    ##################### addr allocate #####################
    def addr_allocate(self, uniid, addr_manager_table):
        # Prefer previously assigned slot matching uniid
        size = len(addr_manager_table)
        for i in range(size):
            if addr_manager_table[i].mapped == 1:
                # Reassign if offline or uninitialized
                if addr_manager_table[i].online == ONLINE_STATE_OFFLINE or addr_manager_table[i].online == ONLINE_STATE_INIT:
                    if uniid == addr_manager_table[i].uniid:
                        addr_manager_table[i].mapped = 1
                        addr_manager_table[i].online = ONLINE_STATE_WAIT_FOR_ACK
                        self.dprintf("alloc method 1 addr %d" % addr_manager_table[i].addr)
                        return addr_manager_table[i].addr
                # If slave answers broadcast while marked online, it reset and needs reassignment
                elif addr_manager_table[i].online == ONLINE_STATE_ONLINE and uniid == addr_manager_table[i].uniid:
                    self.dprintf("Error: addr already allocated, but broadcast ack happened, maybe slave restarted, "
                            "clear the arcked flag and try to allocate the addr again")
                    # Mark slot online once acknowledged
                    addr_manager_table[i].online = ONLINE_STATE_WAIT_FOR_ACK
                    addr_manager_table[i].acked = 0
                    return -1
        # Next try unassigned slots
        for i in range(size):
            if addr_manager_table[i].mapped == 0:
                addr_manager_table[i].mapped = 1
                addr_manager_table[i].online = ONLINE_STATE_WAIT_FOR_ACK 
                addr_manager_table[i].uniid = uniid
                self.uniid_changed = True
                self.dprintf("alloc method 2 addr %d" % addr_manager_table[i].addr)
                return addr_manager_table[i].addr

        # Finally reuse offline slot whose uniid changed
        for i in range(size):
            if addr_manager_table[i].mapped == 1:
                if addr_manager_table[i].online == ONLINE_STATE_OFFLINE or addr_manager_table[i].online == ONLINE_STATE_INIT:
                    if addr_manager_table[i].uniid != uniid:
                        # Overwrite previous uniid
                        addr_manager_table[i].uniid = uniid
                        addr_manager_table[i].mapped = 1
                        addr_manager_table[i].online = ONLINE_STATE_WAIT_FOR_ACK
                        self.uniid_changed = True
                        self.dprintf("alloc method 3 addr %d" % addr_manager_table[i].addr)
                        return addr_manager_table[i].addr
        # Return -1 when no slot is available
        return -1

    def print_buff(self, buff):
        _len = len(buff)
        message = ""
        for i in range(_len):
            message += "0x%02X " % buff[i]
        self.dprintf(message)

    def crc8_cal(self, data, len):
        crc = 0
        for i in range(len):
            crc ^= data[i]
            for j in range(8):
                if crc & 0x80:
                    crc = (crc << 1) ^ POLY
                else:
                    crc <<= 1
                crc &= 0xFF
        return crc

    def cal_pack_crc(self, package):
        crc_buff = [package.length, package.status, package.function_code]
        crc_buff += package.data
        return self.crc8_cal(crc_buff, len(crc_buff))

    def is_dev_type_valid(self, dev_type):
        if dev_type == DEV_TYPE_BTM or dev_type == DEV_TYPE_CLM or dev_type == DEV_TYPE_MB:
            return 1
        else:
            return 0

    def function_code_cb(self, package):
        function_code = package.function_code
        size = 0
        ack_data = None
        if function_code == CMD_SET_SLAVE_ADDR or function_code == CMD_GET_SLAVE_INFO \
            or function_code == CMD_ONLINE_CHECK or function_code == CMD_GET_ADDR_TABLE:
            ack_data = FcAckData(package.data[0], package.data[1], package.data[2:])
        
        ## Record whether any slave reported bootloader mode
        if ack_data is not None:
            addr_manager_table = dev_table_map_table[ack_data.dev_type - DEV_TYPE_INDEX_OFFSET].addr_manager_table
            uniid = ack_data.uniid
            size = dev_table_map_table[ack_data.dev_type - DEV_TYPE_INDEX_OFFSET].size
            for i in range(size):
                if addr_manager_table[i].uniid == uniid:
                    addr_manager_table[i].mode = ack_data.mode 
                    if ack_data.mode == MODE_LOADER:
                        self.dprintf("addr 0x%02X in loader mode" % addr_manager_table[i].addr)
                    break

        if function_code == CMD_SET_SLAVE_ADDR:
            # ack_data = FcAckData(package.data[0], package.data[1], package.data[2:])
            if self.is_dev_type_valid(ack_data.dev_type):
                # self.dprintf("uniid:")
                # self.print_buff(ack_data.uniid)
                addr_manager_table = dev_table_map_table[ack_data.dev_type - DEV_TYPE_INDEX_OFFSET].addr_manager_table
                size = dev_table_map_table[ack_data.dev_type - DEV_TYPE_INDEX_OFFSET].size
                addr = package.slave_addr
                uniid = ack_data.uniid
                self.dprintf("received addr %d uniid %s" % (addr, uniid))
                for i in range(size):
                    self.dprintf("table[%d]: online %d addr %d uniid %s acked %d" % \
                                 (i, addr_manager_table[i].online, addr_manager_table[i].addr, addr_manager_table[i].uniid, addr_manager_table[i].acked))
                    if (addr_manager_table[i].online == ONLINE_STATE_INIT or addr_manager_table[i].online == ONLINE_STATE_WAIT_FOR_ACK) and \
                        addr_manager_table[i].addr == addr and \
                        addr_manager_table[i].uniid == uniid and \
                        addr_manager_table[i].acked == 0:
                        addr_manager_table[i].acked = 1
                        addr_manager_table[i].online = ONLINE_STATE_ONLINE
                        addr_manager_table[i].lost_cnt = 0
                        self.dprintf("addr %d acked" % addr_manager_table[i].addr)
                        break
        elif function_code == CMD_GET_SLAVE_INFO:
            # ack_data = FcAckData(package.data[0], package.data[1], package.data[2:])
            if self.is_dev_type_valid(ack_data.dev_type):
                self.dprintf("dev_type: %d" % ack_data.dev_type)
                self.dprintf("mode: %d" % ack_data.mode)
                self.dprintf("uniid:")
                self.print_buff(ack_data.uniid)
                addr_manager_table = dev_table_map_table[ack_data.dev_type - DEV_TYPE_INDEX_OFFSET].addr_manager_table
                size = dev_table_map_table[ack_data.dev_type - DEV_TYPE_INDEX_OFFSET].size
                addr = self.addr_allocate(ack_data.uniid, addr_manager_table)
                self.dprintf("addr: %d" % addr)

        elif function_code == CMD_ONLINE_CHECK:
            # ack_data = FcAckData(package.data[0], package.data[1], package.data[2:])
            if self.is_dev_type_valid(ack_data.dev_type):
                self.dprintf("uniid:")
                self.print_buff(ack_data.uniid)
                addr_manager_table = dev_table_map_table[ack_data.dev_type - DEV_TYPE_INDEX_OFFSET].addr_manager_table
                size = dev_table_map_table[ack_data.dev_type - DEV_TYPE_INDEX_OFFSET].size
                addr = package.slave_addr
                uniid = ack_data.uniid
                for i in range(size):
                    if addr_manager_table[i].addr == addr and \
                        addr_manager_table[i].uniid == uniid:
                        addr_manager_table[i].acked = 1
                        addr_manager_table[i].online = ONLINE_STATE_ONLINE
                        addr_manager_table[i].lost_cnt = 0
                        self.dprintf("addr %d acked" % addr_manager_table[i].addr)
                        break

        elif function_code == CMD_GET_ADDR_TABLE:
            # ack_data = FcAckData(package.data[0], package.data[1], package.data[2:])
            if self.is_dev_type_valid(ack_data.dev_type):
                self.dprintf("uniid:")
                self.print_buff(ack_data.uniid)
                addr_manager_table = dev_table_map_table[ack_data.dev_type - DEV_TYPE_INDEX_OFFSET].addr_manager_table
                size = dev_table_map_table[ack_data.dev_type - DEV_TYPE_INDEX_OFFSET].size
                addr = package.slave_addr
                uniid = ack_data.uniid
                for i in range(size):
                    if addr_manager_table[i].addr == addr:
                        addr_manager_table[i].uniid = uniid
                        addr_manager_table[i].mapped = 1
                        addr_manager_table[i].acked = 1
                        addr_manager_table[i].online = ONLINE_STATE_ONLINE 
                        addr_manager_table[i].lost_cnt = 0
                        self.uniid_changed = True 
                        self.dprintf("addr %d acked" % addr_manager_table[i].addr)
                        break
        else:
            self.dprintf("unknown function code: %d" % function_code)

        if self.uniid_changed:
            self.uniid_changed = False
            self.save_addr_table_uniids(dev_table_map_table[ack_data.dev_type - DEV_TYPE_INDEX_OFFSET])

    def dprintf(self, msg):
        if self.debug:
            logging.info(msg)

    def data_handler(self, ret):
        # Iterating bytes yields int values
        package = DataPackage(ret[0], ret[1], ret[2], ret[3], ret[4], [b for b in ret[5:-1]], ret[-1])
        if package.status == STATUS_OK:
            self.function_code_cb(package)
        else:
            self.dprintf("Error: status: %d" % package.status)

    def data_pack(self, slave_addr, cmd, data):
        _len = len(data)
        package = DataPackage(PACK_HEAD, slave_addr, _len + 3, STATUS_OK, cmd, data, 0x00)
        package.crc = self.cal_pack_crc(package)
        return package

    def send_package(self, package):
        data_send = bytes([package.slave_addr]) +  \
                    bytes([package.length]) + \
                    bytes([package.status]) + \
                    bytes([package.function_code]) + \
                    bytes(int(c) for c in package.data)
        timeout = cmd_timeout[package.function_code]
        # self.dprintf("data_send: %s" % data_send)
        # self.dprintf("timeout: %f" % timeout)
        ret = self._serial.cmd_send_data_with_response(data_send, timeout, False)
        if ret is None:
            self.dprintf("Error: no response")
            return
        self.dprintf("response is not null")
        # self.dprintf("type of ret is %s" % type(ret))
        self.print_buff(ret)
        # ret has type bytes
        self.data_handler(ret)

    ###################### communication interface ########################
    def communication_get_addr_table(self, addr):
        package = self.data_pack(addr, CMD_GET_ADDR_TABLE, [])
        self.send_package(package)

    def communication_get_slave_info(self, broadcast_addr, send_data):
        package = self.data_pack(broadcast_addr, CMD_GET_SLAVE_INFO, send_data)
        self.send_package(package)

    def communication_set_slave_addr(self, broadcast_addr, addr, uniid):
        send_data = []
        send_data.append(addr)
        send_data += uniid
        # send_data.append(uniid)
        package = self.data_pack(broadcast_addr, CMD_SET_SLAVE_ADDR, send_data)
        self.send_package(package)

    def communication_online_check(self, addr):
        package = self.data_pack(addr, CMD_ONLINE_CHECK, [])
        self.send_package(package)
    
    def communication_loader_check(self, addr):
        package = self.data_pack(addr, CMD_LOADER_TO_APP, [0x01])
        self.send_package(package)

    def print_addr_manager_table(self, addr_manager_table):
        size = len(addr_manager_table)
        for i in range(size):
            log_message = ""
            log_message += "0x%02X, " % addr_manager_table[i].addr
            for j in range(len(addr_manager_table[i].uniid)):
                log_message += "0x%02X, " % addr_manager_table[i].uniid[j]
            log_message += "%d, %d, %d, %d, mode:%d" % (addr_manager_table[i].mapped, addr_manager_table[i].online,
                    addr_manager_table[i].acked, addr_manager_table[i].lost_cnt, addr_manager_table[i].mode)
            self.dprintf(log_message)

    ###################### logic interface ########################
    def get_addr_table(self, dev_table_map):
        self.dprintf("**************************** get addr table ****************************")
        size = dev_table_map.size
        # Skip retries if lower transport already retries
        # Retry here because lower-layer retries are disabled
        for i in range(MAX_GET_TIMES):
            self.dprintf("**** get times %d" % (i + 1))
            for j in range(size):
                # if dev_table_map.addr_manager_table[j].acked == 0:
                if dev_table_map.addr_manager_table[j].online != ONLINE_STATE_ONLINE:
                    self.communication_get_addr_table(dev_table_map.addr_manager_table[j].addr)

            online_slave_num = 0
            for k in range(size):
                if dev_table_map.addr_manager_table[k].online == ONLINE_STATE_ONLINE:
                # if dev_table_map.addr_manager_table[k].acked == 1:
                    online_slave_num += 1
            self.dprintf("online slave num: %d" % online_slave_num)
            # Return early if all slaves already replied
            if online_slave_num == size:
                self.print_addr_manager_table(dev_table_map.addr_manager_table)
                self.dprintf("online slave num is max: %d" % online_slave_num)
                return 
        self.print_addr_manager_table(dev_table_map.addr_manager_table)
    
    def set_addr_table(self, dev_table_map):
        self.dprintf("**************************** set addr table ****************************")
        addr_manager_table = dev_table_map.addr_manager_table
        size = dev_table_map.size
        broadcast_addr = dev_table_map.broadcast_addr
        self.dprintf("before set addr table")
        self.print_addr_manager_table(addr_manager_table) 
        for i in range(MAX_SET_TIMES):
            self.dprintf("**** set times %d" % (i + 1))
            mapped_cnt = 0
            for i in range(size):
                if addr_manager_table[i].mapped == 1:
                    if addr_manager_table[i].online == ONLINE_STATE_INIT:
                        mapped_cnt += 1
                        self.communication_set_slave_addr(broadcast_addr, addr_manager_table[i].addr, addr_manager_table[i].uniid)

            valid_slave_num = 0
            for k in range(size):
                if addr_manager_table[k].online == ONLINE_STATE_ONLINE:
                    valid_slave_num += 1
            self.dprintf("valid slave num: %d" % valid_slave_num)
            # Return early if all devices acknowledged assignment
            if valid_slave_num == mapped_cnt:
                self.print_addr_manager_table(addr_manager_table)
                self.dprintf("valid slave num is max: %d" % valid_slave_num)
                return
        self.print_addr_manager_table(addr_manager_table) 

    def get_slave_info(self, dev_table_map):
        broadcast_addr = dev_table_map.broadcast_addr
        send_data = [broadcast_addr, broadcast_addr]
        online_slave_num = 0
        addr_manager_table = dev_table_map.addr_manager_table 
        size = dev_table_map.size
        self.dprintf("**************************** get slave info ****************************")
        for i in range(size):
            if addr_manager_table[i].online == ONLINE_STATE_ONLINE or addr_manager_table[i].online == ONLINE_STATE_WAIT_FOR_ACK:
                online_slave_num += 1

        ## Skip scan if all devices are online or already queried
        if online_slave_num == size:
            self.dprintf("online slave num is max %d" % online_slave_num)
            return 
        self.communication_get_slave_info(broadcast_addr, send_data)

    def set_slave_addr(self, dev_table_map):
        self.dprintf("**************************** set slave addr ****************************")
        addr_manager_table = dev_table_map.addr_manager_table
        size = dev_table_map.size
        broadcast_addr = dev_table_map.broadcast_addr
        for i in range(size):
            if addr_manager_table[i].mapped == 1:
                if addr_manager_table[i].online == ONLINE_STATE_WAIT_FOR_ACK:
                    self.communication_set_slave_addr(broadcast_addr, addr_manager_table[i].addr, addr_manager_table[i].uniid)

    def online_check(self, dev_table_map):
        self.dprintf("**************************** online check ****************************")
        lost_flag = 0
        mapped_exist = 0
        addr_manager_table = dev_table_map.addr_manager_table 
        size = dev_table_map.size
        for i in range(size):
            if addr_manager_table[i].mapped == 1:
                mapped_exist = 1
                addr_manager_table[i].lost_cnt += 1
                self.communication_online_check(addr_manager_table[i].addr)
                if addr_manager_table[i].lost_cnt > MAX_LOST_CNT:
                    addr_manager_table[i].online = ONLINE_STATE_OFFLINE 
                    addr_manager_table[i].acked = 0
                    self.dprintf("Error: addr %.2X offline" % addr_manager_table[i].addr)
                    lost_flag = 1
        if mapped_exist == 1:
            self.print_addr_manager_table(addr_manager_table)
            if lost_flag == 0:
                self.dprintf("***************** all online ********************")
    def loader_check(self, dev_table_map):
        self.dprintf("**************************** loader check ****************************")
        addr_manager_table = dev_table_map.addr_manager_table 
        size = dev_table_map.size
        for i in range(size):
            # for test
            # addr_manager_table[i].mode = MODE_LOADER
            if addr_manager_table[i].mode == MODE_LOADER:
                ## Command slaves in loader mode to jump to app
                self.communication_loader_check(BROADCAST_ADDR)
                return True 
        return False 
    ###################### reg process ##########################
    def reg_auto_addr_get(self):
        self.reactor.register_callback(self.process_get_addr_table)
    def reg_auto_addr_set(self):
        self.print_stats = self.printer.lookup_object('print_stats')
        self.reactor.register_callback(self.process_set_addr_table)
    def reg_auto_addr_process(self):
        # self.reactor.register_callback(self.process_set_slave_addr)
        # self.reactor.register_callback(self.process_online_check)
        # self.reactor.register_callback(self.process_loader_check)
        self.reactor.register_callback(self.process_all)
        pass


    ###################### process ##########################
    def process_get_addr_table(self, eventtime):
        for i in range(len(dev_table_map_table)):
            dev_table_map = dev_table_map_table[i]
            self.get_addr_table(dev_table_map)
        # self.get_finished = True
        self.dprintf("get addr table finished")
        self.printer.send_event("auto_addr:get_finished")

    def process_set_addr_table(self, eventtime):
        for i in range(len(dev_table_map_table)):
            dev_table_map = dev_table_map_table[i]
            ## Restore addresses from saved uniid table
            self.set_addr_table(dev_table_map)
            ## Re-discover any slaves that missed saved address
            self.get_addr_table(dev_table_map)
        # self.get_finished = True
        self.dprintf("set && get addr table finished")
        self.printer.send_event("auto_addr:set_finished")

    def process_set_slave_addr(self, eventtime):
        while True:
            if self.printer.is_shutdown():
                return
            self.dprintf("set slave addr")
            for i in range(len(dev_table_map_table)):
                dev_table_map = dev_table_map_table[i]
                self.get_slave_info(dev_table_map)
                self.set_slave_addr(dev_table_map)
            self.reactor.pause(self.reactor.monotonic() + 3.)

    def process_online_check(self, eventtime):
        while True:
            time_interval = 1.5
            if self.print_stats.state == "printing" or self.print_stats.state == "pause":
                time_interval = 10
            if self.printer.is_shutdown():
                return
            self.dprintf("online check")
            for i in range(len(dev_table_map_table)):
                dev_table_map = dev_table_map_table[i]
                self.online_check(dev_table_map)
            self.reactor.pause(self.reactor.monotonic() + time_interval)
    def process_loader_check(self, eventtime):
        while True:
            time_interval = 2.0 
            if self.print_stats.state == "printing" or self.print_stats.state == "pause":
                time_interval = 10
            if self.printer.is_shutdown():
                return
            self.dprintf("loader check")
            for i in range(len(dev_table_map_table)):
                dev_table_map = dev_table_map_table[i]
                ## Emit once per poll cycle
                if True == self.loader_check(dev_table_map):
                    break
            self.reactor.pause(self.reactor.monotonic() + time_interval)

    def process_all(self, eventtime):
        while True:
            time_interval = 1.0 
            if self.print_stats.state == "printing" or self.print_stats.state == "pause":
                time_interval = 10
            if self.printer.is_shutdown():
                return

            self.dprintf("set slave addr")
            for i in range(len(dev_table_map_table)):
                dev_table_map = dev_table_map_table[i]
                self.get_slave_info(dev_table_map)
                self.set_slave_addr(dev_table_map)

            self.dprintf("online check")
            for i in range(len(dev_table_map_table)):
                dev_table_map = dev_table_map_table[i]
                self.online_check(dev_table_map)

            self.dprintf("loader check")
            for i in range(len(dev_table_map_table)):
                dev_table_map = dev_table_map_table[i]
                ## Emit once per poll cycle
                if True == self.loader_check(dev_table_map):
                    break
            self.reactor.pause(self.reactor.monotonic() + time_interval)

