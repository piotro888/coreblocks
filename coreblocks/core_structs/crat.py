from amaranth import *
from transactron.core import *
from transactron.lib import BasicFifo
from transactron.utils import from_method_layout

from coreblocks.interface.layouts import RATLayouts
from coreblocks.params.genparams import GenParams


class CheckpointRAT(Elaboratable):
    def __init__(self, gen_params: GenParams):
        self.gen_params = gen_params

        layouts = gen_params.get(RATLayouts)
        self.rename = Method(i=layouts.frat_rename_in, o=layouts.frat_rename_out)
        self.checkpoint_make = Method()
        self.checkpoint_restore = Method()

        self.current_frat = Array([Signal(gen_params.phys_regs_bits) for _ in range(gen_params.isa.reg_cnt)])

        self.storage = Array(
            [
                BasicFifo(from_method_layout([("rp", gen_params.phys_regs_bits)]), gen_params.checkpoint_cnt)
                for _ in range(gen_params.isa.reg_cnt)
            ]
        )
        self.entry_modified = Signal(gen_params.isa.reg_cnt)

        self.index_increment_table = Array([Signal(gen_params.checkpoint_cnt) for _ in range(gen_params.isa.reg_cnt)])

        self.checkpoint_head = Signal(range(gen_params.checkpoint_cnt))
        self.checkpoint_tail = Signal(range(gen_params.checkpoint_cnt))

    def elaborate(self, platform):
        m = TModule()

        def update(rl, rp):
            with m.If(~self.entry_modified[rl]):
                # current_frat entry represents previous checkpoint

                # push old value
                self.storage[rl].write(m, {"rp": self.current_frat[rl]})

                # update index tracking state
                m.d.sync += self.entry_modified[rl].eq(1)
                m.d.sync += self.index_increment_table[rl][self.checkpoint_head].eq(1)

            m.d.sync += self.current_frat[rl].eq(rp)

        @def_method(m, self.rename)
        def _(rp_dst: Value, rl_dst: Value, rl_s1: Value, rl_s2: Value):
            update(rl_dst, rp_dst)
            return {"rp_s1": self.current_frat[rl_s1], "rp_s2": self.current_frat[rl_s2]}

        @def_method(m, self.checkpoint_make)
        def _():
            m.d.sync += self.checkpoint_head.eq(self.checkpoint_head + 1)
            m.d.sync += self.entry_modified.eq(0)  # lazy commit current state on modification

        # TODO: smarter structure
        increment_prefix_sums = Array(
            [
                Array([Signal(range(self.gen_params.checkpoint_cnt)) for _ in range(self.gen_params.checkpoint_cnt)])
                for _ in range(self.gen_params.isa.reg_cnt)
            ]
        )

        for rl in range(self.gen_params.isa.reg_cnt):
            for c in range(self.gen_params.checkpoint_cnt):
                m.d.comb += increment_prefix_sums[rl][c].eq(
                    (increment_prefix_sums[rl][c - 1] if c != 0 else 0) + self.index_increment_table[rl][c]
                )

        def queue_idx_of_checkpoint(rl, checkpoint_idx):
            idx = Signal(range(self.gen_params.checkpoint_cnt))
            # TODO: wrap
            with m.If(checkpoint_idx >= self.checkpoint_tail):
                m.d.av_comb += idx.eq(increment_prefix_sums[rl][checkpoint_idx]-increment_prefix_sums[rl][self.checkpoint_tail])
            with m.Else():
            return increment_prefix_sums[rl][checkpoint_idx]

        def revert_queues(checkpoint_id):
            pass

        @def_method(m, self.checkpoint_restore)
        def _(checkpoint_id):
            revert_queues(checkpoint_id)

        # TOOD: conflicts

        return m
