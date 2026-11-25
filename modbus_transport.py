"""Standard Modbus TCP transport (MBAP, not raw RTU-over-TCP)."""
import socket
import struct
import time
import minimalmodbus as mm

class TCPInstrument:
    def __init__(self, host, port, address, timeout=1.5):
        self.host,self.port,self.address,self.timeout=host,port,address,timeout
        self.transaction=0
        self.serial=self  # close interface shared with the RTU reader

    def close(self):
        pass  # each transaction opens and closes its own connection

    def exchange(self,pdu):
        self.transaction=(self.transaction+1)&65535
        tx=self.transaction
        frame=struct.pack('>HHHB',tx,0,len(pdu)+1,self.address)+pdu
        deadline=time.monotonic()+self.timeout
        with socket.create_connection((self.host,self.port),timeout=self.timeout) as sock:
            def exact(n):
                data=b''
                while len(data)<n:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError('Modbus TCP timeout')
                    sock.settimeout(remaining)
                    chunk=sock.recv(n-len(data))
                    if not chunk:raise OSError('TCP connection closed before the full response')
                    data+=chunk
                return data
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('TCP connection timeout')
            sock.settimeout(remaining)
            sock.sendall(frame)
            got_tx,protocol,length,unit=struct.unpack('>HHHB',exact(7))
            if got_tx!=tx or protocol!=0 or unit!=self.address or not 2<=length<=254:
                raise OSError('Invalid Modbus TCP header (transaction/protocol/unit/length)')
            answer=exact(length-1)
        if answer[0]==(pdu[0]|128):
            if len(answer)!=2:raise OSError('Malformed Modbus TCP exception')
            raise mm.SlaveReportedException(f'Modbus exception 0x{answer[1]:02X}')
        if answer[0]!=pdu[0]:raise OSError('Unexpected Modbus function')
        return answer

    def read_registers(self,address,count,functioncode=3):
        reply=self.exchange(struct.pack('>BHH',functioncode,address,count))
        if len(reply)!=2+count*2 or reply[1]!=count*2:
            raise OSError('Invalid register response length')
        return list(struct.unpack('>'+'H'*count,reply[2:]))

    def read_bits(self,address,count,functioncode=1):
        reply=self.exchange(struct.pack('>BHH',functioncode,address,count))
        size=(count+7)//8
        if len(reply)!=2+size or reply[1]!=size:
            raise OSError('Invalid bit response length')
        return [(reply[2+i//8]>>(i%8))&1 for i in range(count)]

    def write_register(self,address,value,functioncode=6):
        pdu=struct.pack('>BHH',functioncode,address,value)
        if self.exchange(pdu)!=pdu:raise OSError('Invalid FC06 acknowledgement')

    def write_bit(self,address,value,functioncode=5):
        pdu=struct.pack('>BHH',functioncode,address,0xFF00 if value else 0)
        if self.exchange(pdu)!=pdu:raise OSError('Invalid FC05 acknowledgement')

    def write_registers(self,address,values):
        n=len(values)
        reply=self.exchange(struct.pack('>BHHB',16,address,n,n*2)+struct.pack('>'+'H'*n,*values))
        if reply!=struct.pack('>BHH',16,address,n):raise OSError('Invalid register write acknowledgement')

    def write_bits(self,address,values):
        n=len(values);payload=bytearray((n+7)//8)
        for i,v in enumerate(values):payload[i//8]|=(int(v)&1)<<(i%8)
        reply=self.exchange(struct.pack('>BHHB',15,address,n,len(payload))+payload)
        if reply!=struct.pack('>BHH',15,address,n):raise OSError('Invalid bit write acknowledgement')
