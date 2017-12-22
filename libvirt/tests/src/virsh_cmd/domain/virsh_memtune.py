import logging
import re

from autotest.client.shared import error

from virttest import virsh
from virttest import utils_libvirtd
from virttest.staging import utils_memory
from virttest.staging import utils_cgroup
from virttest.libvirt_xml import vm_xml
from virttest.utils_test import libvirt


memtune_types = ['hard-limit', 'soft-limit', 'swap-hard-limit']
memtune_cgnames = [
    'limit_in_bytes',
    'soft_limit_in_bytes',
    'memsw.limit_in_bytes',
]


def check_limit(path, expected_value, limit_type, cgname, vm, test):
    """
    Matches the expected and actual output
    1) Match the output of the virsh memtune
    2) Match the output of the respective cgroup fs value
    3) Match the output of the virsh dumpxml
    4) Check if vm is alive

    :params: path: memory controller path for a domain
    :params: expected_value: the expected limit value
    :params: limit_type: the limit type to be checked
                         hard-limit/soft-limit/swap-hard-limit
    :params: cgname: the cgroup postfix
    :params: vm: vm instance
    :params: test: test instance
    """

    status_value = True
    limit_name = re.sub('-', '_', limit_type)

    # Check 1
    actual_value = virsh.memtune_get(vm.name, limit_name)
    minus = int(expected_value) - int(actual_value)
    if minus > 8:
        status_value = False
        logging.error("%s virsh output:\n\tExpected value:%d"
                      "\n\tActual value: "
                      "%d", limit_name,
                      int(expected_value), int(actual_value))

    # Check 2
    if int(expected_value) != -1:
        cg_file_name = '%s/memory.%s' % (path, cgname)
        cg_file = None
        try:
            try:
                cg_file = open(cg_file_name)
                output = cg_file.read()
                value = int(output) / 1024
                minus = int(expected_value) - int(value)
                if minus > 8:
                    status_value = False
                    logging.error("%s cgroup fs:\n\tExpected Value: %d"
                                  "\n\tActual Value: "
                                  "%d", limit_name,
                                  int(expected_value), int(value))
            except IOError:
                status_value = False
                logging.error("Error while reading:\n%s", cg_file_name)
        finally:
            if cg_file is not None:
                cg_file.close()

    # Check 3
    if int(expected_value) != -1:
        guest_xml = vm_xml.VMXML.new_from_dumpxml(vm.name)
        memtune_element = guest_xml.memtune
        logging.debug("Expected memtune XML is:\n%s", memtune_element)
        actual_fromxml = getattr(memtune_element, limit_name)
        if int(expected_value) != int(actual_fromxml):
            status_value = False
            test.fail("Expect memtune:\n%s\nBut got:\n "
                      "%s" % (expected_value, actual_fromxml))

    # Check 4
    if not vm.is_alive():
        status_value = False
        logging.error("Error: vm is not alive")

    if not status_value:
        test.fail("Failed to restore domain %s" % vm.name)


def check_3_limits(path, mt_limits, vm, test):
    """
    Check 3 types memtune setting in turn
    """
    for index in range(len(memtune_types)):
        check_limit(path, mt_limits[index], memtune_types[index],
                    memtune_cgnames[index], vm, test)


def mem_step(params, path, vm, test):
    # Set the initial memory starting value for test case
    # By default set 1GB less than the total memory
    # In case of total memory is less than 1GB set to 256MB
    # visit subtests.cfg to change these default values
    base_mem = int(params.get("base_mem"))
    hard_base = int(params.get("hard_base_mem"))
    soft_base = int(params.get("soft_base_mem"))

    #Get MemTotal of host
    Memtotal = utils_memory.read_from_meminfo('MemTotal')

    if int(Memtotal) < int(base_mem):
        Mem = int(params.get("min_mem"))
    else:
        Mem = int(Memtotal) - int(base_mem)

    # Run test case with 100kB increase in memory value for each iteration
    while (Mem < Memtotal):
        hard_mem = Mem - hard_base
        soft_mem = Mem - soft_base
        swaphard = Mem

        mt_limits = [str(hard_mem), str(soft_mem), str(swaphard)]
        options = " %s --live" % ' '.join(mt_limits)

        result = virsh.memtune_set(vm.name, options)
        check_3_limits(path, mt_limits, vm, test)

        Mem += hard_base


def run(test, params, env):
    """
    Test the command virsh memtune

    1) To get the current memtune parameters
    2) Change the parameter values
    3) Check the memtune query updated with the values
    4) Check whether the mounted cgroup path gets the updated value
    5) Check the output of virsh dumpxml
    6) Check vm is alive
    """

    # Check for memtune command is available in the libvirt version under test
    if not virsh.has_help_command("memtune"):
        raise error.TestNAError(
            "Memtune not available in this libvirt version")

    # Check if memtune options are supported
    for option in memtune_types:
        if not virsh.has_command_help_match("memtune", option):
            raise error.TestNAError("%s option not available in memtune "
                                    "cmd in this libvirt version" % option)
    # Get common parameters
    step_mem = params.get("step_mem", "no") == "yes"
    expect_error = params.get("expect_error", "no") == "yes"
    restart_libvirtd = params.get("restart_libvirtd", "no") == "yes"
    set_one_line = params.get("set_in_one_command", "no") == "yes"
    mt_hard_limit = params.get("hard_limit", "not set")
    mt_soft_limit = params.get("soft_limit", "not set")
    mt_swap_hard_limit = params.get("swap_hard_limit", "not set")
    # if restart_libvirtd is True, set set_one_line is True
    set_one_line = True if restart_libvirtd else set_one_line

    # Get the vm name, pid of vm and check for alive
    domname = params.get("main_vm")
    vm = env.get_vm(params["main_vm"])
    vm.verify_alive()
    pid = vm.get_pid()

    # Resolve the memory cgroup path for a domain
    path = utils_cgroup.resolve_task_cgroup_path(int(pid), "memory")

    # step_mem is used to do step increment limit testing
    if step_mem:
        mem_step(params, path, vm, test)
        return

    if not set_one_line:
        # set one type memtune limit in one command
        if mt_hard_limit != "not set":
            index = 0
            mt_limit = mt_hard_limit
        elif mt_soft_limit != "not set":
            index = 1
            mt_limit = mt_soft_limit
        elif mt_swap_hard_limit != "not set":
            index = 2
            mt_limit = mt_swap_hard_limit
        mt_type = memtune_types[index]
        mt_cgname = memtune_cgnames[index]
        options = " --%s %s --live" % (mt_type, mt_limit)
        result = virsh.memtune_set(vm.name, options)

        if expect_error:
            fail_patts = [params.get("error_info")]
            libvirt.check_result(result, fail_patts, [])
        else:
            #if limit value is negative, means no memtune limit
            mt_expected = mt_limit if int(mt_limit) > 0 else -1
            check_limit(path, mt_expected, mt_type, mt_cgname, vm, test)
    else:
        # set 3 limits in one command line
        mt_limits = [mt_hard_limit, mt_soft_limit, mt_swap_hard_limit]
        options = " %s --live" % ' '.join(mt_limits)
        result = virsh.memtune_set(vm.name, options)

        if expect_error:
            fail_patts = [params.get("error_info")]
            libvirt.check_result(result, fail_patts, [])
        else:
            check_3_limits(path, mt_limits, vm, test)

        if restart_libvirtd:
            libvirtd = utils_libvirtd.Libvirtd()
            libvirtd.restart()

        if not expect_error:
            #After libvirtd restared, check memtune values again
            check_3_limits(path, mt_limits, vm, test)
