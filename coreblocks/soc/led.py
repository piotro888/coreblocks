from amaranth import *
from amaranth.lib.wiring import Component, In, Out
from amaranth.utils import ceil_log2

from coreblocks.peripherals.wishbone import WishboneInterface, WishboneMuxer, WishboneSignature, WishboneMemorySlave

from .common import add_memory_mapped_register

class LedPeriph(Component):
    bus: WishboneInterface
    led: Signal

    def __init__(self, base_addr: int, *, wb_params, width: int):
        super().__init__(
                {
                    "bus": In(WishboneSignature(wb_params)),
                    "led": Out(width),
                }
        )
        self.base_addr = base_addr
        self.width = width
        self.space_size = 0x4


    def elaborate(self, platform):
        m = Module()

        wb_addr_shift = ceil_log2(self.bus.dat_r.shape().width // 8)
        assert self.width <= 2**wb_addr_shift
        
        reg = Signal(self.width)

        in_range = (self.bus.adr == (self.base_addr >> wb_addr_shift)) 
        
        with m.If(self.bus.stb & self.bus.cyc & in_range):
            m.d.comb += self.bus.ack.eq(1)
            add_memory_mapped_register(m, self.bus, self.base_addr, reg) 
        m.d.comb += self.led.eq(reg)
        
        return m

