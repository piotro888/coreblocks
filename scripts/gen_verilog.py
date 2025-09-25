#!/usr/bin/env python3

import os
import sys
import argparse

from amaranth import *
from transactron.utils.gen import AbstractInterface, verilog


if __name__ == "__main__":
    parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, parent)

from coreblocks.params.genparams import GenParams
from coreblocks.core import Core
from coreblocks.socks.socks import Socks
from transactron import TransactionComponent
from transactron.utils import DependencyManager, DependencyContext

from coreblocks.params.configurations import *

from amaranth.hdl._ir import Design
import amaranth.hdl._mem as _mem


def fixup_vivado_transparent_memories(design: Design):
    # See https://github.com/YosysHQ/yosys/issues/5082
    # Vivado stopped inferring transparent memory ports emitted with Yosys verilog backend
    # correctly from (probably) version 2023.1, printing [Synth 8-6430] warning.
    # It is a Vivado bug, generating circuit behaviour that doesn't match the RTL.
    # It is fixed by adding vivado-specific RTL attribute to main memory declarations that
    # use this pattern.
    # Adds the attribute to all memories with enabled port transparency, needed until (and if)
    # Yosys changes the generated pattern.

    for fragment in design.fragments:  # type: ignore
        if isinstance(fragment, _mem.MemoryInstance):
            is_transparent = any(read_port._transparent_for for read_port in fragment._read_ports)  # type: ignore

            if is_transparent:
                fragment._attrs.setdefault("rw_addr_collision", "yes")  # type: ignore

str_to_coreconfig: dict[str, CoreConfiguration] = {
    "basic": basic_core_config,
    "tiny": tiny_core_config,
    "full": full_core_config,
}

def generate_verilog(
    elaboratable: Elaboratable,
    ports = None,
    top_name: str = "top",
):
    # The ports logic is copied (and simplified) from amaranth.back.verilog.convert.
    # Unfortunately, the convert function doesn't return the name map.
    if ports is None and isinstance(elaboratable, AbstractInterface):
        ports = []
        for _, _, value in elaboratable.signature.flatten(elaboratable):
            ports.append(Value.cast(value))
    elif ports is None:
        raise TypeError("The `generate_verilog()` function requires a `ports=` argument")

    design = Fragment.get(elaboratable, platform=None).prepare(ports=ports)

    fixup_vivado_transparent_memories(design)

    verilog_text, name_map = verilog.convert_fragment(design, name=top_name, emit_src=True, strip_internal_attrs=True)

    return verilog_text


def gen_verilog(core_config: CoreConfiguration, output_path: str, *, wrap_socks: bool = False):
    with DependencyContext(DependencyManager()):
        gp = GenParams(core_config)
        core = Core(gen_params=gp)
        if wrap_socks:
            core = Socks(core, core_gen_params=gp)

        top = TransactionComponent(core, dependency_manager=DependencyContext.get())

        verilog_text = generate_verilog(top)

        with open(output_path, "w") as f:
            f.write(verilog_text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enables verbose output. Default: %(default)s",
    )

    parser.add_argument(
        "-c",
        "--config",
        action="store",
        default="basic",
        help="Select core configuration. "
        + f"Available configurations: {', '.join(list(str_to_coreconfig.keys()))}. Default: %(default)s",
    )

    parser.add_argument(
        "--strip-debug",
        action="store_true",
        help="Remove debugging signals. Default: %(default)s",
    )

    parser.add_argument(
        "--with-socks",
        action="store_true",
        help="Wrap Coreblocks in CoreSoCks providing additional memory-mapped or CSR peripherals",
    )

    parser.add_argument("--reset-pc", action="store", default="0x0", help="Set core reset address")

    parser.add_argument(
        "-o", "--output", action="store", default="core.v", help="Output file path. Default: %(default)s"
    )

    parser.add_argument("--reset-pc", action="store", default="0x0", help="Set core reset address")

    args = parser.parse_args()

    os.environ["AMARANTH_verbose"] = "true" if args.verbose else "false"

    if args.config not in str_to_coreconfig:
        raise KeyError(f"Unknown config '{args.config}'")

    config = str_to_coreconfig[args.config]
    if args.strip_debug:
        config = config.replace(debug_signals=False)

    assert args.reset_pc[:2] == "0x", "Expected hex number as --reset-pc"
    config = config.replace(start_pc=int(args.reset_pc[2:], base=16))

    gen_verilog(config, args.output, wrap_socks=args.with_socks)


if __name__ == "__main__":
    main()
