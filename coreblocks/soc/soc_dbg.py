from amaranth import *
from amaranth.lib.wiring import Component, In, Out, connect, flipped
from amaranth.utils import ceil_log2

from coreblocks.core import Core
from coreblocks.params import GenParams
from coreblocks.peripherals.wishbone import WishboneInterface, WishboneMuxer, WishboneSignature, WishboneMemorySlave
from coreblocks.soc.led import LedPeriph


class DebugSoC(Component):
    led: Signal

    def __init__(self, core: Core, core_gen_params: GenParams):
        super().__init__(
            {
                "led": Out(4)
            }
        )
        
        prog = [
            0xe1000137,
            0x00012083,
            0x00108193,
            0x00312023,
            0xff5ff06f,
        ] + [0] * 8

        self.ledm = LedPeriph(base_addr=0xE1000000, width=4, wb_params=core_gen_params.wb_params)
        self.instr = WishboneMemorySlave(core_gen_params.wb_params, init=prog, depth=len(prog))

        self.core = core
        self.core_gen_params = core_gen_params

    def elaborate(self, platform):
        m = Module()

        muxer_ssel = Signal(2)
        periph_muxer = WishboneMuxer(self.core_gen_params.wb_params, 2, muxer_ssel)

        connect(m, self.core.wb_instr, self.instr.bus)

        connect(m, self.core.wb_data, periph_muxer.master_wb)
        #connect(m, periph_muxer.slaves[0], flipped(self.wb_data))

        connect(m, periph_muxer.slaves[1], self.ledm.bus)

        in_core_periph_space = Signal()
        addr_shift = ceil_log2(self.core.wb_data.dat_r.shape().width // 8)
        m.d.comb += in_core_periph_space.eq(
            (self.core.wb_data.adr >= (self.ledm.base_addr >> addr_shift))
            & (self.core.wb_data.adr < ((self.ledm.base_addr + self.ledm.space_size) >> addr_shift))
        )
        m.d.comb += muxer_ssel.eq(Cat(~in_core_periph_space, in_core_periph_space))

        m.d.comb += self.led.eq(self.ledm.led)
        
        m.submodules.ledm = self.ledm
        m.submodules.instr = self.instr
        m.submodules.periph_muxer = periph_muxer

        m.submodules.core = self.core

        return m
