from errors import RegNotFoundException, RopException

import simuvex


def get_ast_dependency(ast):
    """
    ast must be created from a symbolic state where registers values are named "sreg_REG-"
    looks for registers that if we make the register symbolic then the ast becomes symbolic
    :param ast: the ast of which we are trying to analyze dependencies
    :return: A set of register names which affect the ast
    """
    dependencies = set()

    for var in ast.variables:
        if var.startswith("sreg_"):
            dependencies.add(var[5:].split("-")[0])
        else:
            return []
    return dependencies


def get_ast_controllers(test_state, ast, reg_deps):
    """
    looks for registers that we can make symbolic then the ast can be "anything"
    :param test_state: the input state
    :param ast: the ast of which we are trying to analyze controllers
    :param reg_deps: All registers which it depends on
    :return: A set of register names which can control the ast
    """

    test_val = 0x4141414141414141 % (2 << test_state.arch.bits)

    controllers = []
    if not ast.symbolic:
        return controllers

    # make sure it can't be symbolic if all the registers are constrained
    constrained_copy = test_state.copy()
    for reg in reg_deps:
        if not constrained_copy.registers.load(reg).symbolic:
            continue
        constrained_copy.add_constraints(constrained_copy.registers.load(reg) == test_val)
    if len(constrained_copy.se.any_n_int(ast, 2)) > 1:
        return controllers

    for reg in reg_deps:
        constrained_copy = test_state.copy()
        for r in [a for a in reg_deps if a != reg]:
            # for bp and registers that might be set
            if not constrained_copy.registers.load(r).symbolic:
                continue
            constrained_copy.add_constraints(constrained_copy.registers.load(r) == test_val)

        if unconstrained_check(constrained_copy, ast):
            controllers.append(reg)

    return controllers


def unconstrained_check(state, ast):
    """
    Attempts to check if an ast is completely unconstrained
    :param state: the state to use
    :param ast: the ast to check
    :return: True if the ast is probably completely unconstrained
    """
    size = ast.size()
    test_val_0 = 0x0
    test_val_1 = (1 << size) - 1
    test_val_2 = int("1010"*16, 2) % (1 << size)
    test_val_3 = int("0101"*16, 2) % (1 << size)
    # chars need to be able to be different
    test_val_4 = int(("1001"*2 + "1010"*2 + "1011"*2 + "1100"*2 + "1101"*2 + "1110"*2 + "1110"*2 + "0001"*2), 2) \
        % (1 << size)
    if not state.se.satisfiable(extra_constraints=(ast == test_val_0,)):
        return False
    if not state.se.satisfiable(extra_constraints=(ast == test_val_1,)):
        return False
    if not state.se.satisfiable(extra_constraints=(ast == test_val_2,)):
        return False
    if not state.se.satisfiable(extra_constraints=(ast == test_val_3,)):
        return False
    if not state.se.satisfiable(extra_constraints=(ast == test_val_4,)):
        return False
    return True


def get_reg_name(arch, reg_offset):
    """
    :param reg_offset: Tries to find the name of a register given the offset in the registers.
    :return: The register name
    """
    # todo does this make sense
    if reg_offset is None:
        raise RegNotFoundException("register offset is None")

    original_offset = reg_offset
    while reg_offset >= 0 and reg_offset >= original_offset - (arch.bits/8):
        if reg_offset in arch.register_names:
            return arch.register_names[reg_offset]
        else:
            reg_offset -= 1
    raise RegNotFoundException("register %s not found" % str(original_offset))


# todo this doesn't work if there is a timeout
def _asts_must_be_equal(state, ast1, ast2):
    """
    :param state: the state to use for solving
    :param ast1: first ast
    :param ast2: second ast
    :return: True if the ast's must be equal
    """
    if state.se.satisfiable(extra_constraints=(ast1 != ast2,)):
        return False
    return True


def make_initial_state(project, stack_length):
    """
    :return: an initial state with a symbolic stack and good options for rop
    """
    initial_state = project.factory.blank_state(
        add_options={simuvex.o.AVOID_MULTIVALUED_READS, simuvex.o.AVOID_MULTIVALUED_WRITES,
                     simuvex.o.NO_SYMBOLIC_JUMP_RESOLUTION, simuvex.o.CGC_NO_SYMBOLIC_RECEIVE_LENGTH,
                     simuvex.o.NO_SYMBOLIC_SYSCALL_RESOLUTION, simuvex.o.TRACK_ACTION_HISTORY},
        remove_options=simuvex.o.resilience_options | simuvex.o.simplification)
    initial_state.options.discard(simuvex.o.CGC_ZERO_FILL_UNCONSTRAINED_MEMORY)
    initial_state.options.update({simuvex.o.TRACK_REGISTER_ACTIONS, simuvex.o.TRACK_MEMORY_ACTIONS,
                                  simuvex.o.TRACK_JMP_ACTIONS, simuvex.o.TRACK_CONSTRAINT_ACTIONS})
    symbolic_stack = initial_state.se.BVS("symbolic_stack", project.arch.bits*stack_length)
    initial_state.mem[initial_state.regs.sp:] = symbolic_stack
    initial_state.regs.bp = initial_state.regs.sp + 20*initial_state.arch.bits
    initial_state.se._solver.timeout = 500  # only solve for half a second at most
    return initial_state


def make_symbolic_state(project, reg_list, stack_length=200):
    """
    converts an input state into a state with symbolic registers
    :return: the symbolic state
    """
    input_state = make_initial_state(project, stack_length)
    symbolic_state = input_state.copy()
    # overwrite all registers
    for reg in reg_list:
        symbolic_state.registers.store(reg, symbolic_state.se.BVS("sreg_" + reg + "-", project.arch.bits))
    # restore sp
    symbolic_state.regs.sp = input_state.regs.sp
    # restore bp
    symbolic_state.regs.bp = input_state.regs.bp
    return symbolic_state


def step_to_unconstrained_successor(project, state, path=None, max_steps=2, allow_simprocedures=False):
    """
    steps up to two times to try to find an unconstrained successor
    :param state: the input state
    :param max_steps: maximum number of additional steps to try to get to an unconstrained state
    :return: a path at the unconstrained successor
    """
    try:
        # might only want to enable this option for arches / oses which don't care about bad syscall
        # nums
        state.options.add(simuvex.o.BYPASS_UNSUPPORTED_SYSCALL)
        if path is None:
            p = project.factory.path(state=state)
        else:
            p = path

        if p.errored:
            raise RopException(p.error)

        successors = p.step()
        if len(p.successors) + len(p.unconstrained_successors) != 1:
            raise RopException("Does not get to a single successor")
        if len(p.successors) == 1 and max_steps > 0:
            if not allow_simprocedures and project.is_hooked(p.successors[0].addr):
                raise RopException("Skipping simprocedure")
            return step_to_unconstrained_successor(project, p.successors[0].state, successors[0],
                                                   max_steps-1, allow_simprocedures)
        if len(p.successors) == 1 and max_steps == 0:
            raise RopException("Does not get to an unconstrained successor")
        p = p.unconstrained_successors[0]
        return p

    except simuvex.UnsupportedSyscallError:
        raise RopException("Does not get to a single unconstrained successor")