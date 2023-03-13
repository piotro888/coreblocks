from amaranth import Elaboratable, Module

from coreblocks.fu.alu import ALUComponent
from coreblocks.fu.jumpbranch import JumpComponent
from coreblocks.lsu.dummyLsu import LSUBlockComponent
from coreblocks.stages.rs_func_block import RSBlockComponent
from coreblocks.transactions import TransactionModule
from coreblocks.transactions.lib import AdapterTrans

from .common import TestCaseWithSimulator, TestbenchIO

from coreblocks.core import Core
from coreblocks.params import GenParams
from coreblocks.peripherals.wishbone import WishboneMaster, WishboneMemorySlave, WishboneParameters

from typing import Optional
import random
import subprocess
import tempfile
from parameterized import parameterized_class
from riscvmodel.insn import (
    InstructionADDI,
    InstructionSLTI,
    InstructionSLTIU,
    InstructionXORI,
    InstructionORI,
    InstructionANDI,
    InstructionSLLI,
    InstructionSRLI,
    InstructionSRAI,
    InstructionLUI,
)
from riscvmodel.model import Model
from riscvmodel.isa import InstructionRType, get_insns
from riscvmodel.variant import RV32I


_BASIC_CONFIGURATION = [RSBlockComponent([ALUComponent(), JumpComponent()], rs_entries=4), LSUBlockComponent()]


class TestElaboratable(Elaboratable):
    def __init__(self, gen_params: GenParams, instr_mem: list[int] = [], data_mem: Optional[list[int]] = None):
        self.gp = gen_params
        self.instr_mem = instr_mem
        if data_mem is None:
            self.data_mem = [0] * (2**10)
        else:
            self.data_mem = data_mem

    def elaborate(self, platform):
        m = Module()
        tm = TransactionModule(m)

        wb_params = WishboneParameters(data_width=32, addr_width=30)
        self.wb_master_instr = WishboneMaster(wb_params=wb_params)
        self.wb_master_data = WishboneMaster(wb_params=wb_params)
        self.wb_mem_slave = WishboneMemorySlave(
            wb_params=wb_params, width=32, depth=len(self.instr_mem), init=self.instr_mem
        )
        self.wb_mem_slave_data = WishboneMemorySlave(
            wb_params=wb_params, width=32, depth=len(self.data_mem), init=self.data_mem
        )
        self.core = Core(gen_params=self.gp, wb_master_instr=self.wb_master_instr, wb_master_data=self.wb_master_data)
        self.io_in = TestbenchIO(AdapterTrans(self.core.fifo_fetch.write))
        self.rf_write = TestbenchIO(AdapterTrans(self.core.RF.write))

        m.submodules.wb_master_instr = self.wb_master_instr
        m.submodules.wb_master_data = self.wb_master_data
        m.submodules.wb_mem_slave = self.wb_mem_slave
        m.submodules.wb_mem_slave_data = self.wb_mem_slave_data
        m.submodules.c = self.core
        m.submodules.io_in = self.io_in
        m.submodules.rf_write = self.rf_write

        m.d.comb += self.wb_master_instr.wbMaster.connect(self.wb_mem_slave.bus)
        m.d.comb += self.wb_master_data.wbMaster.connect(self.wb_mem_slave_data.bus)

        return tm


def gen_riscv_add_instr(dst, src1, src2):
    return 0b0110011 | dst << 7 | src1 << 15 | src2 << 20


def gen_riscv_lui_instr(dst, imm):
    return 0b0110111 | dst << 7 | imm << 12


class TestCoreBase(TestCaseWithSimulator):
    gp: GenParams
    m: TestElaboratable

    def check_RAT_alloc(self, rat, expected_alloc_count=None):  # noqa: N802
        allocated = []
        for i in range(self.m.gp.isa.reg_cnt):
            allocated.append((yield rat.entries[i]))
        filtered_zeros = list(filter(lambda x: x != 0, allocated))

        # check if 0th register is set to 0
        self.assertEqual(allocated[0], 0)
        # check if there are no duplicate physical registers allocated for two different architectural registers
        self.assertEqual(len(filtered_zeros), len(set(filtered_zeros)))
        # check if the expected number of allocated registers matches reality
        if expected_alloc_count:
            self.assertEqual(len(filtered_zeros), expected_alloc_count)

    def get_phys_reg_rrat(self, reg_id):
        return (yield self.m.core.RRAT.entries[reg_id])

    def get_phys_reg_frat(self, reg_id):
        return (yield self.m.core.FRAT.entries[reg_id])

    def get_arch_reg_val(self, reg_id):
        return (yield self.m.core.RF.entries[(yield from self.get_phys_reg_rrat(reg_id))].reg_val)

    def get_phys_reg_val(self, reg_id):
        return (yield self.m.core.RF.entries[reg_id].reg_val)

    def push_instr(self, opcode):
        yield from self.m.io_in.call({"data": opcode})

    def compare_core_states(self, sw_core):
        for i in range(self.gp.isa.reg_cnt):
            reg_val = sw_core.state.intreg.regs[i].value
            unsigned_val = reg_val & 0xFFFFFFFF
            self.assertEqual((yield from self.get_arch_reg_val(i)), unsigned_val)


class TestCoreSimple(TestCoreBase):
    def simple_test(self):
        # this test first provokes allocation of physical registers,
        # then sets the values in those registers, and finally runs
        # an actual computation.

        # The test sets values in the reg file by hand

        # provoking allocation of physical register
        for i in range(self.m.gp.isa.reg_cnt - 1):
            yield from self.push_instr(gen_riscv_add_instr(i + 1, 0, 0))

        # waiting for the retirement rat to be set
        for i in range(50):
            yield

        # checking if all registers have been allocated
        yield from self.check_RAT_alloc(self.m.core.FRAT, 31)
        yield from self.check_RAT_alloc(self.m.core.RRAT, 31)

        # writing values to physical registers
        yield from self.m.rf_write.call({"reg_id": (yield from self.get_phys_reg_rrat(1)), "reg_val": 1})
        yield from self.m.rf_write.call({"reg_id": (yield from self.get_phys_reg_rrat(2)), "reg_val": 2})
        yield from self.m.rf_write.call({"reg_id": (yield from self.get_phys_reg_rrat(3)), "reg_val": 3})

        # waiting for potential conflicts on rf_write
        for i in range(10):
            yield

        self.assertEqual((yield from self.get_arch_reg_val(1)), 1)
        self.assertEqual((yield from self.get_arch_reg_val(2)), 2)
        self.assertEqual((yield from self.get_arch_reg_val(3)), 3)

        # issuing actual instructions for the test
        yield from self.push_instr(gen_riscv_add_instr(4, 1, 2))
        yield from self.push_instr(gen_riscv_add_instr(4, 3, 4))
        yield from self.push_instr(gen_riscv_lui_instr(5, 1))

        # waiting for the instructions to be processed
        for i in range(50):
            yield

        self.assertEqual((yield from self.get_arch_reg_val(1)), 1)
        self.assertEqual((yield from self.get_arch_reg_val(2)), 2)
        self.assertEqual((yield from self.get_arch_reg_val(3)), 3)
        # 1 + 2 + 3 = 6
        self.assertEqual((yield from self.get_arch_reg_val(4)), 6)
        self.assertEqual((yield from self.get_arch_reg_val(5)), 1 << 12)

    def test_simple(self):
        gp = GenParams(
            "rv32i",
            _BASIC_CONFIGURATION,
            phys_regs_bits=6,
            rob_entries_bits=7,
        )
        m = TestElaboratable(gp)
        self.m = m

        with self.run_simulation(m) as sim:
            sim.add_sync_process(self.simple_test)


class TestCoreRandomized(TestCoreBase):
    def randomized_input(self):
        halt_pc = len(self.instr_mem) * self.gp.isa.ilen_bytes

        # set PC to halt at specific instruction (numbered from 0)
        yield self.m.core.fetch.halt_pc.eq(halt_pc)

        # wait for PC to go past all instruction
        while (yield self.m.core.fetch.pc) < halt_pc:
            yield

        # finish calculations
        for _ in range(50):
            yield

        yield from self.compare_core_states(self.software_core)

    def test_randomized(self):
        self.gp = GenParams(
            "rv32i",
            _BASIC_CONFIGURATION,
            phys_regs_bits=6,
            rob_entries_bits=7,
        )
        self.instr_count = 300
        random.seed(42)

        instructions = get_insns(cls=InstructionRType, variant=RV32I)
        instructions += [
            InstructionADDI,
            InstructionSLTI,
            InstructionSLTIU,
            InstructionXORI,
            InstructionORI,
            InstructionANDI,
            InstructionSLLI,
            InstructionSRLI,
            InstructionSRAI,
            InstructionLUI,
        ]

        # allocate some random values for registers
        init_instr_list = list(
            InstructionADDI(rd=i, rs1=0, imm=random.randint(-(2**11), 2**11 - 1))
            for i in range(self.gp.isa.reg_cnt)
        )

        # generate random instruction stream
        instr_list = list(random.choice(instructions)() for _ in range(self.instr_count))
        for instr in instr_list:
            instr.randomize(RV32I)

        self.software_core = Model(RV32I)
        self.software_core.execute(init_instr_list)
        self.software_core.execute(instr_list)

        self.instr_mem = list(map(lambda x: x.encode(), init_instr_list + instr_list))

        m = TestElaboratable(self.gp, instr_mem=self.instr_mem)
        self.m = m

        with self.run_simulation(m) as sim:
            sim.add_sync_process(self.randomized_input)


@parameterized_class(
    ("name", "source_file", "instr_count", "expected_regvals"),
    [("fibonacci", "fibonacci.asm", 1200, {2: 2971215073}), ("fibonacci_mem", "fibonacci_mem.asm", 500, {3: 55})],
)
class TestCoreAsmSource(TestCoreBase):
    source_file: str
    instr_count: int
    expected_regvals: dict[int, int]

    def run_and_check(self):
        for i in range(self.instr_count):
            yield

        for reg_id, val in self.expected_regvals.items():
            self.assertEqual((yield from self.get_arch_reg_val(reg_id)), val)

    def test_asm_source(self):
        self.gp = GenParams("rv32i", _BASIC_CONFIGURATION)
        self.base_dir = "test/asm/"
        self.bin_src = []

        with tempfile.NamedTemporaryFile() as asm_tmp:
            subprocess.check_call(
                [
                    "riscv64-unknown-elf-as",
                    "-mabi=ilp32",
                    "-march=rv32i",
                    "-o",
                    asm_tmp.name,
                    self.base_dir + self.source_file,
                ]
            )
            code = subprocess.check_output(
                ["riscv64-unknown-elf-objcopy", "-O", "binary", "-j", ".text", asm_tmp.name, "/dev/stdout"]
            )
            for word_idx in range(0, len(code), 4):
                word = code[word_idx : word_idx + 4]
                bin_instr = int.from_bytes(word, "little")
                self.bin_src.append(bin_instr)

        self.m = TestElaboratable(self.gp, instr_mem=self.bin_src)
        with self.run_simulation(self.m) as sim:
            sim.add_sync_process(self.run_and_check)
