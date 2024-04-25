from amaranth import *
from coreblocks.interface.layouts import CheckpointQueueLayouts, RATLayouts

from coreblocks.params import GenParams
from transactron.core.method import Method
from transactron.core.sugar import def_method
from transactron.core.tmodule import TModule
from transactron.lib.fifo import BasicFifo
from transactron.utils.amaranth_ext.elaboratables import OneHotSwitch, OneHotSwitchDynamic
from transactron.utils.transactron_helpers import make_layout


class CheckpointQueue(Elaboratable):
    def __init__(self, gen_params: GenParams, length: int): 
        self.gen_params = gen_params
        self.layout = make_layout(("phys_reg", gen_params.phys_regs_bits), ("checkpoint_id", range(gen_params.checkpoint_count)))
        layouts = gen_params.get(CheckpointQueueLayouts)

        self.checkpoint = Method() 
        self.rename = Method(i=layouts.rename)
        self.peek = Method(o=layouts.peek, nonexclusive=True)
        
        self.storage = Array([Signal(self.layout) for _ in range(length)])
        self.end_idx = Signal(range(length))
        self.start_idx = Signal(range(length))
    
        self.checkpoint_id = Signal(range(gen_params.checkpoint_count))

    def elaborate(self, platform):
        m = TModule()
        
        checkpoint_id_sync = Signal.like(self.checkpoint_id)
        m.d.comb += self.checkpoint_id.eq(checkpoint_id_sync)
        @def_method(m, self.checkpoint)
        def _(checkpoint_id):
            m.d.comb += checkpoint_id.eq(checkpoint_id) 
            m.d.sync += checkpoint_id_sync.eq(checkpoint_id)

        @def_method(m, self.peek)
        def _():
            return self.storage[self.end_idx].phys_reg

        @def_method(m, self.rename)
        def _(rl_dst):
            with m.If(self.storage[self.end_idx].checkpoint_id != self.checkpoint_id):
                m.d.sync += self.storage[self.end_idx + 1].phys_reg.eq(rl_dst) # TODO: wind
                m.d.sync += self.end_idx.eq(self.end_idx + 1)
            with m.Else():    
                m.d.sync += self.storage[self.end_idx].phys_reg.eq(rl_dst)
        
        @def_method(m, self.free)
        def _():
            # check next id
            pass

        def find_last_le(id: Value) -> Value: 
            id_lt = Signal(self.gen_params.checkpoint_count)
            for i, e in enumerate(self.storage):
                # ehh id will wrap
                m.d.comb += id_lt[i].eq(e.checkpoint_id <= id)
            

            ## TODO: compare preformance with recursive structur
            result = Signal.like(self.checkpoint_id) 
            
            ####### :c
            ### i dont like it
            

        @def_method(m, self.restore)
        def _(checkpoint_id):



        return m

class CheckpointRAT(Elaboratable):
    def __init__(self, gen_params: GenParams):
        self.queues = [CheckpointQueue(gen_params, gen_params.checkpoint_count) for _ in range(gen_params.isa.reg_cnt)]

        rat_layouts = gen_params.get(RATLayouts)
        self.rename = Method(i=rat_layouts.frat_rename_in, o=rat_layouts.frat_rename_out)

        self.checkpoint_id = Signal(range(gen_params.checkpoint_count))

    def elaborate(self, platform):
        m = TModule()

        table = Array(self.queues)

        for i, q in enumerate(self.queues):
            m.submodules[f"rl_{i}_queue"] = q

        @def_method(m, self.rename)
        def _(rp_dst: Value, rl_dst: Value, rl_s1: Value, rl_s2: Value):
            # FIXME
            with OneHotSwitch(m, rl_dst) as Case:
                for i, q in enumerate(self.queues):
                    with Case(i):
                        m.d.sync += q.rename(m, rp_dst)

            return {"rp_s1":  table[rl_s1].peek(m), "rp_s2": table[rl_s2].peek(m)}

        ## TODO: check checkpoint_id availbility === or maybe as opt do it in individual queues? 

        

