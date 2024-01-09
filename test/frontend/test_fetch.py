from collections import deque

import random

from amaranth import Elaboratable, Module
from amaranth.sim import Passive

from transactron.core import Method
from transactron.lib import AdapterTrans, FIFO, Adapter
from coreblocks.frontend.fetch import Fetch, UnalignedFetch
from coreblocks.frontend.icache import ICacheInterface
from coreblocks.params import *
from coreblocks.params.configurations import test_core_config
from transactron.utils import ModuleConnector
from ..common import TestCaseWithSimulator, TestbenchIO, def_method_mock


class MockedICache(Elaboratable, ICacheInterface):
    def __init__(self, gen_params: GenParams):
        layouts = gen_params.get(ICacheLayouts)

        self.issue_req_io = TestbenchIO(Adapter(i=layouts.issue_req))
        self.accept_res_io = TestbenchIO(Adapter(o=layouts.accept_res))

        self.issue_req = self.issue_req_io.adapter.iface
        self.accept_res = self.accept_res_io.adapter.iface
        self.flush = Method()

    def elaborate(self, platform):
        m = Module()

        m.submodules.issue_req_io = self.issue_req_io
        m.submodules.accept_res_io = self.accept_res_io

        return m


class TestElaboratable(Elaboratable):
    def __init__(self, gen_params: GenParams):
        self.gen_params = gen_params

    def elaborate(self, platform):
        m = Module()

        self.icache = MockedICache(self.gen_params)

        fifo = FIFO(self.gen_params.get(FetchLayouts).raw_instr, depth=2)
        self.io_out = TestbenchIO(AdapterTrans(fifo.read))
        self.fetch = Fetch(self.gen_params, self.icache, fifo.write)
        self.fetch_resume = TestbenchIO(AdapterTrans(self.fetch.resume))
        self.fetch_stall_exception = TestbenchIO(AdapterTrans(self.fetch.stall_exception))

        m.submodules.icache = self.icache
        m.submodules.fetch = self.fetch
        m.submodules.io_out = self.io_out
        m.submodules.fetch_resume = self.fetch_resume
        m.submodules.fetch_stall_exception = self.fetch_stall_exception
        m.submodules.fifo = fifo

        return m


class TestFetch(TestCaseWithSimulator):
    def setUp(self) -> None:
        self.gen_params = GenParams(test_core_config.replace(start_pc=0x18))
        self.m = TestElaboratable(self.gen_params)
        self.instr_queue = deque()
        self.iterations = 500

        random.seed(422)

    def cache_processes(self):
        input_q = deque()
        output_q = deque()

        def cache_process():
            yield Passive()

            next_pc = self.gen_params.start_pc

            while True:
                while len(input_q) == 0:
                    yield

                while random.random() < 0.5:
                    yield

                addr = input_q.popleft()
                is_branch = random.random() < 0.15

                # exclude branches and jumps
                data = random.randrange(2**self.gen_params.isa.ilen) & ~0b1111111

                # randomize being a branch instruction
                if is_branch:
                    data |= 0b1100000
                    data &= ~0b0010000  # but not system

                output_q.append({"instr": data, "error": 0})

                # Speculative fetch. Skip, because this instruction shouldn't be executed.
                if addr != next_pc:
                    continue

                next_pc = addr + self.gen_params.isa.ilen_bytes
                if is_branch:
                    next_pc = random.randrange(2**self.gen_params.isa.ilen) & ~0b11

                self.instr_queue.append(
                    {
                        "instr": data,
                        "pc": addr,
                        "is_branch": is_branch,
                        "next_pc": next_pc,
                    }
                )

        @def_method_mock(lambda: self.m.icache.issue_req_io, enable=lambda: len(input_q) < 2, sched_prio=1)
        def issue_req_mock(addr):
            input_q.append(addr)

        @def_method_mock(lambda: self.m.icache.accept_res_io, enable=lambda: len(output_q) > 0)
        def accept_res_mock():
            return output_q.popleft()

        return issue_req_mock, accept_res_mock, cache_process

    def fetch_out_check(self):
        discard_mispredict = False
        next_pc = 0
        for _ in range(self.iterations):
            v = yield from self.m.io_out.call()
            if discard_mispredict:
                while v["pc"] != next_pc:
                    v = yield from self.m.io_out.call()
                discard_mispredict = False

            while len(self.instr_queue) == 0:
                yield

            instr = self.instr_queue.popleft()

            self.assertEqual(v["pc"], instr["pc"])
            self.assertEqual(v["instr"], instr["instr"])

            if instr["is_branch"]:
                # branches on mispredict will stall fetch because of exception and then resume with new pc
                yield from self.random_wait(5)
                yield from self.m.fetch_stall_exception.call()
                yield from self.random_wait(5)
                yield from self.m.fetch_resume.call(pc=instr["next_pc"], resume_from_exception=1)
                discard_mispredict = True
                next_pc = instr["next_pc"]

    def test(self):
        issue_req_mock, accept_res_mock, cache_process = self.cache_processes()

        with self.run_simulation(self.m) as sim:
            sim.add_sync_process(issue_req_mock)
            sim.add_sync_process(accept_res_mock)
            sim.add_sync_process(cache_process)
            sim.add_sync_process(self.fetch_out_check)


class TestUnalignedFetch(TestCaseWithSimulator):
    def setUp(self) -> None:
        self.gen_params = GenParams(test_core_config.replace(start_pc=0x18, compressed=True))
        self.instr_queue = deque()
        self.instructions = 500

        self.icache = MockedICache(self.gen_params)
        fifo = FIFO(self.gen_params.get(FetchLayouts).raw_instr, depth=2)
        self.io_out = TestbenchIO(AdapterTrans(fifo.read))
        fetch = UnalignedFetch(self.gen_params, self.icache, fifo.write)
        self.fetch_resume = TestbenchIO(AdapterTrans(fetch.resume))
        self.fetch_stall_exception = TestbenchIO(AdapterTrans(fetch.stall_exception))

        self.m = ModuleConnector(self.icache, fifo, self.io_out, fetch, self.fetch_resume, self.fetch_stall_exception)

        self.mem = {}
        self.memerr = set()

        random.seed(422)

    def gen_instr_seq(self):
        pc = self.gen_params.start_pc

        for _ in range(self.instructions):
            is_branch = random.random() < 0.15
            is_rvc = random.random() < 0.5
            branch_target = random.randrange(2**self.gen_params.isa.ilen) & ~0b1

            error = random.random() < 0.1

            if is_rvc:
                data = (random.randrange(11) << 2) | 0b01  # C.ADDI
                if is_branch:
                    data = data | (0b101 << 13)  # make it C.J

                self.mem[pc] = data
                if error:
                    self.memerr.add(pc)
            else:
                data = random.randrange(2**self.gen_params.isa.ilen) & ~0b1111111
                data |= 0b11  # 2 lowest bits must be set in 32-bit long instructions
                if is_branch:
                    data |= 0b1100000
                    data &= ~0b0010000

                self.mem[pc] = data & 0xFFFF
                self.mem[pc + 2] = data >> 16
                if error:
                    self.memerr.add(random.choice([pc, pc + 2]))

            next_pc = pc + (2 if is_rvc else 4)
            if is_branch:
                next_pc = branch_target

            self.instr_queue.append(
                {
                    "pc": pc,
                    "is_branch": is_branch,
                    "next_pc": next_pc,
                    "rvc": is_rvc,
                }
            )

            pc = next_pc

    def cache_processes(self):
        input_q = deque()
        output_q = deque()

        def cache_process():
            yield Passive()

            while True:
                while len(input_q) == 0:
                    yield

                while random.random() < 0.5:
                    yield

                req_addr = input_q.popleft()

                def get_mem_or_random(addr):
                    return self.mem[addr] if addr in self.mem else random.randrange(2**16)

                data = (get_mem_or_random(req_addr + 2) << 16) | get_mem_or_random(req_addr)

                err = (req_addr in self.memerr) or (req_addr + 2 in self.memerr)
                output_q.append({"instr": data, "error": err})

        @def_method_mock(lambda: self.icache.issue_req_io, enable=lambda: len(input_q) < 2, sched_prio=1)
        def issue_req_mock(addr):
            input_q.append(addr)

        @def_method_mock(lambda: self.icache.accept_res_io, enable=lambda: len(output_q) > 0)
        def accept_res_mock():
            return output_q.popleft()

        return issue_req_mock, accept_res_mock, cache_process

    def fetch_out_check(self):
        discard_mispredict = False
        while self.instr_queue:
            instr = self.instr_queue.popleft()

            instr_error = (instr["pc"] & (~0b11)) in self.memerr or (instr["pc"] & (~0b11)) + 2 in self.memerr
            if instr["pc"] & 2 and not instr["rvc"]:
                instr_error |= ((instr["pc"] + 2) & (~0b11)) in self.memerr or (
                    (instr["pc"] + 2) & (~0b11)
                ) + 2 in self.memerr

            v = yield from self.io_out.call()
            if discard_mispredict:
                while v["pc"] != instr["pc"]:
                    v = yield from self.io_out.call()
                discard_mispredict = False

            self.assertEqual(v["pc"], instr["pc"])
            self.assertEqual(v["access_fault"], instr_error)

            if instr["is_branch"] or instr_error:
                yield from self.random_wait(5)
                yield from self.fetch_stall_exception.call()
                yield from self.random_wait(5)
                while (yield from self.fetch_resume.call_try(pc=instr["next_pc"], resume_from_exception=1)) is None:
                    yield from self.io_out.call_try()  # try flushing to unblock or wait

                discard_mispredict = True

    def test(self):
        issue_req_mock, accept_res_mock, cache_process = self.cache_processes()

        self.gen_instr_seq()

        with self.run_simulation(self.m) as sim:
            sim.add_sync_process(issue_req_mock)
            sim.add_sync_process(accept_res_mock)
            sim.add_sync_process(cache_process)
            sim.add_sync_process(self.fetch_out_check)
