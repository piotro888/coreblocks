from amaranth import *

class RomPeriph(Component):
    bus: WishboneInterface

    def __init__(self, base_addr: int, *, wb_params: WishboneParams, contents: bytes, space_size: Optional[int] = None):
        super().__init__(
                {
                    "bus": In(WishboneSignature(wb_params))
                }
        )
        self.base_addr = base_addr
        self.rom = contents
        self.space_size = len(contents) if space_size is None else space_size

    def elaborate(self, platform):
        wb_addr_shift = ceil_log2(self.bus.dat_r.shape().width // 8)
        in_range = (self.bus.adr >= (self.base_adr >> wb_addr_shift)) & (self.bus.adr < ((self.base_adr + self.space_size + 2**wb_adr_shift - 1) >> wb_adr_shift))
        
        with m.If(self.bus.stb & self.bus.cyc & in_range):
            in_contents_range = ((self.bus.adr << wb_addr_shift) - self.base_addr) > len(contents)
            m.d.comb += self.bus.ack(in_contents_range)
            m.d.comb += self.bus.err(~in_contents_range)

        m.d.comb += self.bus.dat_r.eq(self.rom[self.bus.adr << wb_addr_shift : (self.bus.adr << wb_addr_shift) + (self.bus.dat_r.shape().width // 8)]) 

