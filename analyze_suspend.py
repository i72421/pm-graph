#!/usr/bin/python
#
# Tool for analyzing suspend/resume timing
# Copyright (c) 2013, Intel Corporation.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms and conditions of the GNU General Public License,
# version 2, as published by the Free Software Foundation.
#
# This program is distributed in the hope it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 
# 51 Franklin St - Fifth Floor, Boston, MA 02110-1301 USA.
#
# Authors:
#     Todd Brandt <todd.e.brandt@intel.com>
#
# Description:
#     This tool is designed to assist kernel and OS developers in optimizing
#     their linux stack's suspend/resume time. Using a kernel image built 
#     with a few extra options enabled and a small patch to enable ftrace, 
#     the tool will execute a suspend, and will capture dmesg and ftrace
#     data until resume is complete. This data is transformed into a set of 
#     timelines and a callgraph to give a quick and detailed view of which
#     devices and kernel processes are taking the most time in suspend/resume.
#     
#     The following kernel build options are required:
#         CONFIG_PM_DEBUG=y
#         CONFIG_PM_SLEEP_DEBUG=y
#
#     The following additional kernel parameters are required:
#         (e.g. in file /etc/default/grub)
#         GRUB_CMDLINE_LINUX_DEFAULT="... initcall_debug log_buf_len=16M ..."
#
#     The following simple patch must be applied to enable ftrace data:
#         in file: kernel/power/suspend.c
#         in function: int suspend_devices_and_enter(suspend_state_t state)
#         remove call to "ftrace_stop();"
#         remove call to "ftrace_start();"
#

import sys
import time
import os
import string
import tempfile
import re
import array
import platform
import datetime
from collections import namedtuple

# -- classes --

class SystemValues:
    testdir = "."
    tpath = "/sys/kernel/debug/tracing/"
    powerfile = "/sys/power/state"
    suspendmode = "mem"
    prefix = "test"
    teststamp = ""
    dmesgfile = ""
    ftracefile = ""
    htmlfile = ""
    def __init__(self):
        hostname = platform.node()
        if(hostname != ""):
            self.prefix = hostname
    def setTestStamp(self):
        self.teststamp = "# "+self.testdir+" "+self.prefix+" "+self.suspendmode
    def setTestFiles(self):
        self.dmesgfile = self.testdir+"/"+self.prefix+"_"+self.suspendmode+"_dmesg.txt"
        self.ftracefile = self.testdir+"/"+self.prefix+"_"+self.suspendmode+"_ftrace.txt"
        self.htmlfile = self.testdir+"/"+self.prefix+"_"+self.suspendmode+".html"
    def setOutputFile(self):
        if((self.htmlfile == "") and (self.dmesgfile != "")):
            m = re.match(r"(?P<name>.*)_dmesg\.txt$", self.dmesgfile)
            if(m):
                self.htmlfile = m.group("name")+".html"
        if((self.htmlfile == "") and (self.ftracefile != "")):
            m = re.match(r"(?P<name>.*)_ftrace\.txt$", self.ftracefile)
            if(m):
                self.htmlfile = m.group("name")+".html"
        if(self.htmlfile == ""):
            self.htmlfile = "output.html"
    def initTestOutput(self):
        self.testdir = os.popen("date \"+suspend-%m%d%y-%H%M%S\"").read().strip()
        self.setTestStamp()
        self.setTestFiles()
        os.mkdir(self.testdir)

class Data:
    usedmesg = False
    useftrace = False
    notestrun = False
    verbose = False
    phases = []
    dmesg = {} # root data structure
    start = 0.0
    end = 0.0
    stamp = {'time': "", 'host': "", 'mode': ""}
    id = 0
    def initialize(self):
        self.dmesg = { # dmesg log data
                'suspend_general': {'list': dict(), 'start': -1.0,        'end': -1.0,
                                     'row': 0,      'color': "#CCFFCC", 'order': 0},
                  'suspend_early': {'list': dict(), 'start': -1.0,        'end': -1.0,
                                     'row': 0,      'color': "green",   'order': 1},
                  'suspend_noirq': {'list': dict(), 'start': -1.0,        'end': -1.0,
                                     'row': 0,      'color': "#00FFFF", 'order': 2},
                    'suspend_cpu': {'list': dict(), 'start': -1.0,        'end': -1.0,
                                     'row': 0,      'color': "blue",    'order': 3},
                     'resume_cpu': {'list': dict(), 'start': -1.0,        'end': -1.0,
                                     'row': 0,      'color': "red",     'order': 4},
                   'resume_noirq': {'list': dict(), 'start': -1.0,        'end': -1.0,
                                     'row': 0,      'color': "orange",  'order': 5},
                   'resume_early': {'list': dict(), 'start': -1.0,        'end': -1.0,
                                     'row': 0,      'color': "yellow",  'order': 6},
                 'resume_general': {'list': dict(), 'start': -1.0,        'end': -1.0,
                                     'row': 0,      'color': "#FFFFCC", 'order': 7}
        }
        self.phases = self.sortedPhases()
    def vprint(self, msg):
        if(self.verbose):
            print(msg)
    def dmesgSortVal(self, phase):
        return self.dmesg[phase]['order']
    def sortedPhases(self):
        return sorted(self.dmesg, key=self.dmesgSortVal)
    def sortedDevices(self, phase):
        list = self.dmesg[phase]['list']
        slist = []
        tmp = dict()
        for devname in list:
            dev = list[devname]
            tmp[dev['start']] = devname
        for t in sorted(tmp):
            slist.append(tmp[t])
        return slist
    def fixupInitcalls(self, phase, end):
        # if any calls never returned, clip them at system resume end
        phaselist = self.dmesg[phase]['list']
        for devname in phaselist:
            dev = phaselist[devname]
            if(dev['end'] < 0):
                dev['end'] = end
                self.vprint("%s (%s): callback didn't return" % (devname, phase))
    def fixupInitcallsThatDidntReturn(self):
        # if any calls never returned, clip them at system resume end
        for phase in self.phases:
            self.fixupInitcalls(phase, self.dmesg['resume_general']['end'])
            if(phase == "resume_general"):
                break
    def newDeviceCallback(self, phase, name, pid, parent, start, end):
        self.id += 1
        devid = "dc%d" % self.id
        list = self.dmesg[phase]['list']
        length = -1.0
        if(start >= 0 and end >= 0):
            length = end - start
        list[name] = {'start': start, 'end': end, 'pid': pid, 'par': parent, 
                      'length': length, 'row': 0, 'id': devid }
    def deviceChildren(self, devname, phase):
        devlist = [devname]
        for p in self.phases:
            if(p[0] != phase[0]):
                continue
            list = data.dmesg[p]['list']
            for child in list:
                if(list[child]['par'] == devname):
                    devlist += self.deviceChildren(child, phase)
        return devlist
    def deviceParent(self, devname, phase):
        devlist = [devname]
        for p in self.phases:
            if(p[0] != phase[0]):
                continue
            list = data.dmesg[p]['list']
            if devname in list:
                devlist += self.deviceParent(list[devname]['par'], phase)
                break
        return devlist
    def deviceTree(self, devname, phase):
        if "cpu" in phase:
            return []
        clist = self.deviceChildren(devname, phase)
        plist = self.deviceParent(devname, phase)
        if(phase[0] == 's'):
            devlist = plist
            for d in clist:
                if(d != devname):
                    devlist.insert(0, d)
        else:
            devlist = clist
            for d in plist:
                if(d != devname):
                    devlist.insert(0, d)
        return devlist
    def deviceIDs(self, devlist):
        idlist = []
        for p in self.phases:
            list = data.dmesg[p]['list']
            for devname in list:
                if devname in devlist:
                    idlist.append(list[devname]['id'])
        return idlist

class FTraceLine:
    time = 0.0
    length = 0.0
    fcall = False
    freturn = False
    depth = 0
    name = ""
    def __init__(self, t, m, d):
        self.time = float(t)
        if(d):
            self.length = float(d)/1000000
        match = re.match(r"^(?P<d> *)(?P<o>.*)$", m)
        if(not match):
            return
        self.depth = self.getDepth(match.group('d'))
        m = match.group('o')
        # function return
        if(m[0] == '}'):
            self.freturn = True
            if(len(m) > 1):
                # includes comment with function name
                match = re.match(r"^} *\/\* *(?P<n>.*) *\*\/$", m)
                if(match):
                    self.name = match.group('n')
        # function call
        else:
            self.fcall = True
            # function call with children
            if(m[-1] == '{'):
                match = re.match(r"^(?P<n>.*) *\(.*", m)
                if(match):
                    self.name = match.group('n')
            # function call with no children (leaf)
            elif(m[-1] == ';'):
                self.freturn = True
                match = re.match(r"^(?P<n>.*) *\(.*", m)
                if(match):
                    self.name = match.group('n')
            # something else (possibly a trace marker)
            else:
                self.name = m
    def getDepth(self, str):
        return len(str)/2

class FTraceCallGraph:
    start = -1.0
    end = -1.0
    list = []
    invalid = False
    depth = 0
    def __init__(self):
        self.start = -1.0
        self.end = -1.0
        self.list = []
        self.depth = 0
    def setDepth(self, line):
        if(line.fcall and not line.freturn):
            line.depth = self.depth
            self.depth += 1
        elif(line.freturn and not line.fcall):
            self.depth -= 1
            line.depth = self.depth
        else:
            line.depth = self.depth
    def addLine(self, line, match):
        if(not self.invalid):
            self.setDepth(line)
        if(line.depth == 0 and line.freturn):
            self.end = line.time
            self.list.append(line)
            return True
        if(self.invalid):
            return False
        if(len(self.list) >= 1000000 or self.depth < 0):
           first = self.list[0]
           self.list = []
           self.list.append(first)
           self.invalid = True
           id = "task %s cpu %s" % (match.group("pid"), match.group("cpu"))
           window = "(%f - %f)" % (self.start, line.time)
           data.vprint("Too much data for "+id+" "+window+", ignoring this callback")
           return False
        self.list.append(line)
        if(self.start < 0):
            self.start = line.time
        return False
    def sanityCheck(self):
        stack = dict()
        cnt = 0
        for l in self.list:
            if(l.fcall and not l.freturn):
                stack[l.depth] = l
                cnt += 1
            elif(l.freturn and not l.fcall):
                if(not stack[l.depth]):
                    return False
                stack[l.depth].length = l.length
                stack[l.depth] = 0
                l.length = 0
                cnt -= 1
        if(cnt == 0):
            return True
        return False
    def debugPrint(self, filename):
        if(filename == "stdout"):
            print("[%f - %f]") % (self.start, self.end)
            for l in self.list:
                if(l.freturn and l.fcall):
                    print("%f (%02d): %s(); (%.3f us)" % (l.time, l.depth, l.name, l.length*1000000))
                elif(l.freturn):
                    print("%f (%02d): %s} (%.3f us)" % (l.time, l.depth, l.name, l.length*1000000))
                else:
                    print("%f (%02d): %s() { (%.3f us)" % (l.time, l.depth, l.name, l.length*1000000))
            print(" ")
        else:
            fp = open(filename, 'w')
            print(filename)
            for l in self.list:
                if(l.freturn and l.fcall):
                    fp.write("%f (%02d): %s(); (%.3f us)\n" % (l.time, l.depth, l.name, l.length*1000000))
                elif(l.freturn):
                    fp.write("%f (%02d): %s} (%.3f us)\n" % (l.time, l.depth, l.name, l.length*1000000))
                else:
                    fp.write("%f (%02d): %s() { (%.3f us)\n" % (l.time, l.depth, l.name, l.length*1000000))
            fp.close()

class Timeline:
    html = {}
    scaleH = 0.0 # height of the timescale row as a percent of the timeline height
    rowH = 0.0 # height of each row in percent of the timeline height
    row_height_pixels = 30
    maxrows = 0
    height = 0
    def __init__(self):
        self.html = {
            'timeline': "",
            'legend': "",
            'scale': ""
        }
    def setRows(self, rows):
        self.maxrows = int(rows)
	self.scaleH = 100.0/float(self.maxrows)
        self.height = self.maxrows*self.row_height_pixels
        r = float(self.maxrows - 1)
        if(r < 1.0):
            r = 1.0
        self.rowH = (100.0 - self.scaleH)/r

# -- global objects --

sysvals = SystemValues()
data = Data()

# -- functions --

# Function: initFtrace
# Description:
#     Configure ftrace to capture a function trace during suspend/resume
def initFtrace():
    global sysvals

    print("INITIALIZING FTRACE...")
    # turn trace off
    os.system("echo 0 > "+sysvals.tpath+"tracing_on")
    # set the trace clock to global
    os.system("echo global > "+sysvals.tpath+"trace_clock")
    # set trace buffer to a huge value
    os.system("echo nop > "+sysvals.tpath+"current_tracer")
    os.system("echo 100000 > "+sysvals.tpath+"buffer_size_kb")
    # clear the trace buffer
    os.system("echo \"\" > "+sysvals.tpath+"trace")
    # set trace type
    os.system("echo function_graph > "+sysvals.tpath+"current_tracer")
    os.system("echo \"\" > "+sysvals.tpath+"set_ftrace_filter")
    # set trace format options
    os.system("echo funcgraph-abstime > "+sysvals.tpath+"trace_options")
    os.system("echo funcgraph-proc > "+sysvals.tpath+"trace_options")
    # focus only on device suspend and resume
    os.system("cat "+sysvals.tpath+"available_filter_functions | grep dpm_run_callback > "+sysvals.tpath+"set_graph_function")

# Function: verifyFtrace
# Description:
#     Check that ftrace is working on the system
def verifyFtrace():
    global sysvals
    files = ["available_filter_functions", "buffer_size_kb",
             "current_tracer", "set_ftrace_filter", 
             "trace", "trace_marker"]
    for f in files:
        if(os.path.exists(sysvals.tpath+f) == False):
            print("ERROR: Missing %s") % (sysvals.tpath+f)
            return False
    return True

def parseStamp(line):
    global data
    stampfmt = r"# suspend-(?P<m>[0-9]{2})(?P<d>[0-9]{2})(?P<y>[0-9]{2})-"+\
                "(?P<H>[0-9]{2})(?P<M>[0-9]{2})(?P<S>[0-9]{2})"+\
                " (?P<host>.*) (?P<mode>.*)$"
    m = re.match(stampfmt, line)
    if(m):
       dt = datetime.datetime(int(m.group("y"))+2000, int(m.group("m")),
            int(m.group("d")), int(m.group("H")), int(m.group("M")),
            int(m.group("S")))
       data.stamp['time'] = dt.strftime("%B %d %Y, %I:%M:%S %p")
       data.stamp['host'] = m.group("host")
       data.stamp['mode'] = m.group("mode") 

# Function: analyzeTraceLog
# Description:
#     Analyse an ftrace log output file generated from this app during
#     the execution phase. Create an "ftrace" structure in memory for
#     subsequent formatting in the html output file
def analyzeTraceLog():
    global sysvals, data

    # the ftrace data is tied to the dmesg data
    if(not data.usedmesg):
        return

    # read through the ftrace and parse the data
    data.vprint("Analyzing the ftrace data...")
    ftrace_line_fmt = r"^ *(?P<time>[0-9\.]*) *\| *(?P<cpu>[0-9]*)\)"+\
                       " *(?P<proc>.*)-(?P<pid>[0-9]*) *\|"+\
                       "[ +!]*(?P<dur>[0-9\.]*) .*\|  (?P<msg>.*)"
    ftemp = dict()
    inthepipe = False
    tf = open(sysvals.ftracefile, 'r')
    count = 0
    for line in tf:
        count = count + 1
        # grab the time stamp if it's valid
        if(count == 1):
            parseStamp(line)
            continue
        # parse only valid lines
        m = re.match(ftrace_line_fmt, line)
        if(not m):
            continue
        m_time = m.group("time")
        m_pid = m.group("pid")
        m_msg = m.group("msg")
        m_dur = m.group("dur")
        if(m_time and m_pid and m_msg):
            t = FTraceLine(m_time, m_msg, m_dur)
            pid = int(m_pid)
        else:
            continue
        # only parse the ftrace data during suspend/resume
        if(not inthepipe):
            # look for the suspend start marker
            if(t.name == "/* SUSPEND START */"):
                data.vprint("SUSPEND START %f %s:%d" % (t.time, sysvals.ftracefile, count))
                inthepipe = True
        else:
            # look for the resume end marker
            if(t.name == "/* RESUME COMPLETE */"):
                data.vprint("RESUME COMPLETE %f %s:%d" % (t.time, sysvals.ftracefile, count))
                inthepipe = False
                break
            # create a callgraph object for the data
            if(pid not in ftemp):
                ftemp[pid] = FTraceCallGraph()
            # when the call is finished, see which device matches it
            if(ftemp[pid].addLine(t, m)):
                if(not ftemp[pid].sanityCheck()):
                    id = "task %s cpu %s" % (pid, m.group("cpu"))
                    data.vprint("Sanity check failed for "+id+", ignoring this callback")
                    continue
                callstart = ftemp[pid].start
                callend = ftemp[pid].end
                for p in data.phases:
                    if(data.dmesg[p]['start'] <= callstart and callstart <= data.dmesg[p]['end']):
                        list = data.dmesg[p]['list']
                        for devname in list:
                            dev = list[devname]
                            if(pid == dev['pid'] and callstart <= dev['start'] and callend >= dev['end']):
                                data.vprint("%15s [%f - %f] %s(%d)" % (p, callstart, callend, devname, pid))
                                dev['ftrace'] = ftemp[pid]
                        break
                ftemp[pid] = FTraceCallGraph()
    tf.close()

# Function: sortKernelLog
# Description:
#     The dmesg output log sometimes comes with with lines that have
#     timestamps out of order. This could cause issues since a call
#     could accidentally end up in the wrong phase
def sortKernelLog():
    global sysvals
    lf = open(sysvals.dmesgfile, 'r')
    dmesglist = []
    first = True
    for line in lf:
        if(first):
            first = False
            parseStamp(line)
        if(re.match(r"(\[ *)(?P<ktime>[0-9\.]*)(\]) (?P<msg>.*)", line)):
            dmesglist.append(line)
    lf.close()
    dmesglist.sort()
    last = ""
    # fix lines with the same time stamp and function with the call and return swapped
    for line in dmesglist:
        mc = re.match(r"(\[ *)(?P<t>[0-9\.]*)(\]) calling  (?P<f>.*)\+ @ .*, parent: .*", line)
        mr = re.match(r"(\[ *)(?P<t>[0-9\.]*)(\]) call (?P<f>.*)\+ returned .* after (?P<dt>.*) usecs", last)
        if(mc and mr and (mc.group("t") == mr.group("t")) and (mc.group("f") == mr.group("f"))):
            i = dmesglist.index(last)
            j = dmesglist.index(line)
            dmesglist[i] = line
            dmesglist[j] = last
        last = line
    return dmesglist

# Function: analyzeKernelLog
# Description:
#     Analyse a dmesg log output file generated from this app during
#     the execution phase. Create a set of device structures in memory 
#     for subsequent formatting in the html output file
def analyzeKernelLog():
    global sysvals, data

    print("PROCESSING DATA")
    data.vprint("Analyzing the dmesg data...")
    if(os.path.exists(sysvals.dmesgfile) == False):
        print("ERROR: %s doesn't exist") % sysvals.dmesgfile
        return False

    lf = sortKernelLog()
    state = "suspend_runtime"

    cpususpend_start = 0.0
    for line in lf:
        # parse each dmesg line into the time and message
        m = re.match(r"(\[ *)(?P<ktime>[0-9\.]*)(\]) (?P<msg>.*)", line)
        if(m):
            ktime = float(m.group("ktime"))
            msg = m.group("msg")
        else:
            continue

        # ignore everything until we're in a suspend/resume
        if(state not in data.phases):
            # suspend start
            if(re.match(r"PM: Syncing filesystems.*", msg)):
                state = "suspend_general"
                data.dmesg[state]['start'] = ktime
                data.start = ktime
            continue

        # suspend_early
        if(re.match(r"PM: suspend of devices complete after.*", msg)):
            data.dmesg[state]['end'] = ktime
            state = "suspend_early"
            data.dmesg[state]['start'] = ktime
        # suspend_noirq
        elif(re.match(r"PM: late suspend of devices complete after.*", msg)):
            data.dmesg[state]['end'] = ktime
            state = "suspend_noirq"
            data.dmesg[state]['start'] = ktime
        # suspend_cpu
        elif(re.match(r"ACPI: Preparing to enter system sleep state.*", msg)):
            data.dmesg[state]['end'] = ktime
            state = "suspend_cpu"
            data.dmesg[state]['start'] = ktime
        # resume_cpu
        elif(re.match(r"ACPI: Low-level resume complete.*", msg)):
            data.dmesg[state]['end'] = ktime
            state = "resume_cpu"
            data.dmesg[state]['start'] = ktime
        # resume_noirq
        elif(re.match(r"ACPI: Waking up from system sleep state.*", msg)):
            data.dmesg[state]['end'] = ktime
            state = "resume_noirq"
            data.dmesg[state]['start'] = ktime
        # resume_early
        elif(re.match(r"PM: noirq resume of devices complete after.*", msg)):
            data.dmesg[state]['end'] = ktime
            state = "resume_early"
            data.dmesg[state]['start'] = ktime
        # resume_general
        elif(re.match(r"PM: early resume of devices complete after.*", msg)):
            data.dmesg[state]['end'] = ktime
            state = "resume_general"
            data.dmesg[state]['start'] = ktime
        # resume complete
        elif(re.match(r".*Restarting tasks .* done.*", msg)):
            data.dmesg[state]['end'] = ktime
            data.end = ktime
            state = "resume_runtime"
            break
        # device init call
        elif(re.match(r"calling  (?P<f>.*)\+ @ .*, parent: .*", msg)):
            if(state not in data.phases):
                print("IGNORING - %f: %s") % (ktime, msg)
                continue
            sm = re.match(r"calling  (?P<f>.*)\+ @ (?P<n>.*), parent: (?P<p>.*)", msg);
            f = sm.group("f")
            n = sm.group("n")
            p = sm.group("p")
            if(f and n and p):
                data.newDeviceCallback(state, f, int(n), p, ktime, -1)
        # device init return
        elif(re.match(r"call (?P<f>.*)\+ returned .* after (?P<t>.*) usecs", msg)):
            if(state not in data.phases):
                print("IGNORING - %f: %s") % (ktime, msg)
                continue
            sm = re.match(r"call (?P<f>.*)\+ returned .* after (?P<t>.*) usecs(?P<a>.*)", msg);
            f = sm.group("f")
            t = sm.group("t")
            list = data.dmesg[state]['list']
            if(f in list):
                dev = list[f]
                dev['length'] = int(t)
                dev['end'] = ktime
                data.vprint("%15s [%f - %f] %s(%d) %s" % 
                    (state, dev['start'], dev['end'], f, dev['pid'], dev['par']))
        # suspend_cpu - cpu suspends
        elif(state == "suspend_cpu"):
            if(re.match(r"Disabling non-boot CPUs .*", msg)):
                cpususpend_start = ktime
                continue
            m = re.match(r"smpboot: CPU (?P<cpu>[0-9]*) is now offline", msg)
            if(m):
                list = data.dmesg[state]['list']
                cpu = "CPU"+m.group("cpu")
                data.newDeviceCallback(state, cpu, 0, "", cpususpend_start, ktime)
                cpususpend_start = ktime
                continue
        # suspend_cpu - cpu suspends
        elif(state == "resume_cpu"):
            list = data.dmesg[state]['list']
            m = re.match(r"smpboot: Booting Node (?P<node>[0-9]*) Processor (?P<cpu>[0-9]*) .*", msg)
            if(m):
                cpu = "CPU"+m.group("cpu")
                data.newDeviceCallback(state, cpu, 0, "", ktime, ktime)
                continue
            m = re.match(r"CPU(?P<cpu>[0-9]*) is up", msg)
            if(m):
                cpu = "CPU"+m.group("cpu")
                list[cpu]['end'] = ktime
                list[cpu]['length'] = ktime - list[cpu]['start']
                continue

    data.fixupInitcallsThatDidntReturn()
    return True

# Function: setTimelineRows
# Description:
#     Organize the device or thread lists into the smallest
#     number of rows possible, with no entry overlapping
# Arguments:
#     list: the list to sort (dmesg or ftrace)
#     sortedkeys: sorted key list to use
def setTimelineRows(list, sortedkeys):
    global data

    # clear all rows and set them to undefined
    remaining = len(list)
    rowdata = dict()
    row = 0
    for item in list:
        list[item]['row'] = -1

    # try to pack each row with as many ranges as possible
    while(remaining > 0):
        if(row not in rowdata):
            rowdata[row] = []
        for item in sortedkeys:
            if(list[item]['row'] < 0):
                s = list[item]['start']
                e = list[item]['end']
                valid = True
                for ritem in rowdata[row]:
                    rs = ritem['start']
                    re = ritem['end']
                    if(not (((s <= rs) and (e <= rs)) or ((s >= re) and (e >= re)))):
                        valid = False
                        break
                if(valid):
                    rowdata[row].append(list[item])
                    list[item]['row'] = row
                    remaining -= 1
        row += 1
    return row

# Function: createTimeScale
# Description:
#     Create timescale lines for the dmesg and ftrace timelines
# Arguments:
#     t0: start time (suspend begin)
#     tMax: end time (resume end)
#     tSuspend: time when suspend occurs
def createTimeScale(t0, tMax, tSuspended):
    global data
    timescale = "<div class=\"t\" style=\"right:{0}%\">{1}</div>\n"
    output = ""

    # set scale for timeline
    tTotal = tMax - t0
    tS = 0.1
    if(tTotal <= 0):
        return output
    if(tTotal > 4):
        tS = 1
    if(tSuspended < 0):
        for i in range(int(tTotal/tS)+1):
            pos = "%0.3f" % (100 - ((float(i)*tS*100)/tTotal))
            if(i > 0):
                val = "%0.f" % (float(i)*tS*1000)
            else:
                val = ""
            output += timescale.format(pos, val)
    else:
        tSuspend = tSuspended - t0
        divTotal = int(tTotal/tS) + 1
        divSuspend = int(tSuspend/tS)
        s0 = (tSuspend - tS*divSuspend)*100/tTotal
        for i in range(divTotal):
            pos = "%0.3f" % (100 - ((float(i)*tS*100)/tTotal) - s0)
            if((i == 0) and (s0 < 3)):
                val = ""
            elif(i == divSuspend):
                val = "S/R"
            else:
                val = "%0.f" % (float(i-divSuspend)*tS*1000)
            output += timescale.format(pos, val)
    return output

# Function: createHTML
# Description:
#     Create the output html file.
def createHTML():
    global sysvals, data

    # html function templates
    headline_stamp = '<div class="stamp">{0} host({1}) mode({2})</div>\n'
    headline_dmesg = '<h1>Kernel {0} Timeline (Suspend {1} ms, Resume {2} ms)</h1>\n'
    html_timeline = '<div id="{0}" class="timeline" style="height:{1}px">\n'
    html_device = '<div id="{0}" title="{1}" class="thread" style="left:{2}%;top:{3}%;height:{4}%;width:{5}%;">{6}</div>\n'
    html_phase = '<div class="phase" style="left:{0}%;width:{1}%;top:{2}%;height:{3}%;background-color:{4}">{5}</div>\n'
    html_legend = '<div class="square" style="left:{0}%;background-color:{1}">&nbsp;{2}</div>\n'

    # device timeline (dmesg)
    if(data.usedmesg):
        data.vprint("Creating Device Timeline...")
        devtl = Timeline()

        # Generate the header for this timeline
        t0 = data.start
        tMax = data.end
        tTotal = tMax - t0
        suspend_time = "%.0f"%((data.dmesg['suspend_cpu']['end'] - data.dmesg['suspend_general']['start'])*1000)
        resume_time = "%.0f"%((data.dmesg['resume_general']['end'] - data.dmesg['resume_cpu']['start'])*1000)
        devtl.html['timeline'] = headline_dmesg.format("Device", suspend_time, resume_time)

        # determine the maximum number of rows we need to draw
        timelinerows = 0
        for phase in data.dmesg:
            list = data.dmesg[phase]['list']
            rows = setTimelineRows(list, list)
            data.dmesg[phase]['row'] = rows
            if(rows > timelinerows):
                timelinerows = rows

        # calculate the timeline height and create its bounding box
	devtl.setRows(timelinerows + 1)
        devtl.html['timeline'] += html_timeline.format("dmesg", devtl.height);

        # draw the colored boxes for each of the phases
        for b in data.dmesg:
            phase = data.dmesg[b]
            left = "%.3f" % (((phase['start']-data.start)*100)/tTotal)
            width = "%.3f" % (((phase['end']-phase['start'])*100)/tTotal)
            devtl.html['timeline'] += html_phase.format(left, width, "%.3f"%devtl.scaleH, "%.3f"%(100-devtl.scaleH), data.dmesg[b]['color'], "")

        # draw the time scale, try to make the number of labels readable
        devtl.html['scale'] = createTimeScale(t0, tMax, data.dmesg['suspend_cpu']['end'])
        devtl.html['timeline'] += devtl.html['scale']
        for b in data.dmesg:
            phaselist = data.dmesg[b]['list']
            for d in phaselist:
                dev = phaselist[d]
                height = (100.0 - devtl.scaleH)/data.dmesg[b]['row']
                top = "%.3f" % ((dev['row']*height) + devtl.scaleH)
                left = "%.3f" % (((dev['start']-data.start)*100)/tTotal)
                width = "%.3f" % (((dev['end']-dev['start'])*100)/tTotal)
                len = " (%0.3f ms) " % ((dev['end']-dev['start'])*1000)
                color = "rgba(204,204,204,0.5)"
                devtl.html['timeline'] += html_device.format(dev['id'], d+len+b, left, top, "%.3f"%height, width, d)

        # timeline is finished
        devtl.html['timeline'] += "</div>\n"

        # draw a legend which describes the phases by color
        devtl.html['legend'] = "<div class=\"legend\">\n"
        for phase in data.phases:
            order = "%.2f" % ((data.dmesg[phase]['order'] * 12.5) + 4.25)
            name = string.replace(phase, "_", " &nbsp;")
            devtl.html['legend'] += html_legend.format(order, data.dmesg[phase]['color'], name)
        devtl.html['legend'] += "</div>\n"

    hf = open(sysvals.htmlfile, 'w')
    thread_height = 0

    # write the html header first (html head, css code, everything up to the start of body)
    html_header = "<!DOCTYPE html>\n<html>\n<head>\n\
    <meta http-equiv=\"content-type\" content=\"text/html; charset=UTF-8\">\n\
    <title>AnalyzeSuspend</title>\n\
    <style type='text/css'>\n\
        .stamp {width: 100%; height: 30px;text-align:center;background-color:gray;line-height:30px;color:white;font: 25px Arial;}\n\
        .callgraph {margin-top: 30px;box-shadow: 5px 5px 20px black;}\n\
        .callgraph article * {padding-left: 28px;}\n\
        .hide {display: none;}\n\
        .pf {display: none;}\n\
        .pf:checked + label {background: url(\'data:image/svg+xml;utf,<?xml version=\"1.0\" standalone=\"no\"?><svg xmlns=\"http://www.w3.org/2000/svg\" height=\"18\" width=\"18\" version=\"1.1\"><circle cx=\"9\" cy=\"9\" r=\"8\" stroke=\"black\" stroke-width=\"1\" fill=\"white\"/><rect x=\"4\" y=\"8\" width=\"10\" height=\"2\" style=\"fill:black;stroke-width:0\"/><rect x=\"8\" y=\"4\" width=\"2\" height=\"10\" style=\"fill:black;stroke-width:0\"/></svg>\') no-repeat left center;}\n\
        .pf:not(:checked) ~ label {background: url(\'data:image/svg+xml;utf,<?xml version=\"1.0\" standalone=\"no\"?><svg xmlns=\"http://www.w3.org/2000/svg\" height=\"18\" width=\"18\" version=\"1.1\"><circle cx=\"9\" cy=\"9\" r=\"8\" stroke=\"black\" stroke-width=\"1\" fill=\"white\"/><rect x=\"4\" y=\"8\" width=\"10\" height=\"2\" style=\"fill:black;stroke-width:0\"/></svg>\') no-repeat left center;}\n\
        .pf:checked ~ *:not(:nth-child(2)) {display: none;}\n\
        .timeline {position: relative; font-size: 14px;cursor: pointer;width: 100%; overflow: hidden; box-shadow: 5px 5px 20px black;}\n\
        .thread {position: absolute; height: "+"%.3f"%thread_height+"%; overflow: hidden; line-height: 30px; border:1px solid;text-align:center;white-space:nowrap;background-color:rgba(204,204,204,0.5);}\n\
        .thread:hover {background-color:white;border:1px solid red;z-index:10;}\n\
        .phase {position: absolute;overflow: hidden;border:0px;text-align:center;}\n\
        .t {position: absolute; top: 0%; height: 100%; border-right:1px solid black;}\n\
        .legend {position: relative; width: 100%; height: 40px; text-align: center;margin-bottom:20px}\n\
        .legend .square {position:absolute;top:10px; width: 0px;height: 20px;border:1px solid;padding-left:20px;}\n\
    </style>\n</head>\n<body>\n"
    hf.write(html_header)

    # write the test title and general info header
    if(data.stamp['time'] != ""):
        hf.write(headline_stamp.format(data.stamp['time'], data.stamp['host'],
                                       data.stamp['mode']))

    # write the dmesg data (device timeline)
    if(data.usedmesg):
        hf.write(devtl.html['timeline'])
        hf.write(devtl.html['legend'])
        hf.write('<div id="devicedetail"></div>\n')
        hf.write('<div id="devicetree"></div>\n')

    # write the ftrace data (callgraph)
    if(data.useftrace):
        hf.write('<section id="callgraphs" class="callgraph">\n')
        # write out the ftrace data converted to html
        html_func_top = '<article id="{0}" class="atop" style="background-color:{1}">\n<input type="checkbox" class="pf" id="f{2}" checked/><label for="f{2}">{3} {4} {5}</label>\n'
        html_func_start = '<article>\n<input type="checkbox" class="pf" id="f{0}" checked/><label for="f{0}">{1} {2} {3}</label>\n'
        html_func_end = '</article>\n'
        html_func_leaf = '<article>{0} {1} {2}</article>\n'
        num = 0
        for p in data.phases:
            list = data.dmesg[p]['list']
            for devname in data.sortedDevices(p):
                if('ftrace' not in list[devname]):
                    continue
                devid = list[devname]['id']
                cg = list[devname]['ftrace']
                flen = "(%.3f ms)" % ((cg.end - cg.start)*1000)
                ftime = " [%.6f - %.6f]" % (cg.start, cg.end)
                hf.write(html_func_top.format(devid, data.dmesg[p]['color'], num, devname+" "+p, flen, ftime))
                num += 1
                for line in cg.list:
                    if(line.length < 0.000000001):
                        flen = ""
                    else:
                        flen = "(%.3f us)" % (line.length*1000000)
                    ftime = "(%.6f)" % line.time
                    if(line.freturn and line.fcall):
                        hf.write(html_func_leaf.format(line.name, flen, ftime))
                    elif(line.freturn):
                        hf.write(html_func_end)
                    else:
                        hf.write(html_func_start.format(num, line.name, flen, ftime))
                        num += 1
                hf.write(html_func_end)
        hf.write("\n\n    </section>\n")
    # write the footer and close
    addScriptCode(hf)
    hf.write("</body>\n</html>\n")
    hf.close()
    return True

def addScriptCode(hf):
    global data
    dfmt = '   d["%s"] = {tree:"%s", filter:[%s]};\n';
    detail = '   var d = [];\n'
    for p in data.dmesg:
        list = data.dmesg[p]['list']
        for d in list:
            tstr = ""
            idstr = ""
            tree = data.deviceTree(d, p)
            idlist = data.deviceIDs(tree)
            for i in tree:
                if(tstr == ""):
                    tstr += i
                else:
                    tstr += " &rarr; "+i
            for i in idlist:
                if(idstr == ""):
                    idstr += '"'+i+'"'
                else:
                    idstr += ', '+'"'+i+'"'
            detail += dfmt % (list[d]['id'], tstr, idstr)
    script_code = \
    '<script type="text/javascript">\n'+detail+\
    '   function deviceDetail() {\n'\
    '       var devtitle = document.getElementById("devicedetail");\n'\
    '       devtitle.innerHTML = "<h1>Device detail for "+this.title+"</h1>";\n'\
    '       var devtree = document.getElementById("devicetree");\n'\
    '       devtree.innerHTML = "<b>"+d[this.id].tree+"</b>";\n'\
    '       var cglist = document.getElementById("callgraphs");\n'\
    '       if(!cglist) return;\n'\
    '       var cg = cglist.getElementsByClassName("atop");\n'\
    '       for (var i = 0; i < cg.length; i++) {\n'\
    '           if(d[this.id].filter.indexOf(cg[i].id) >= 0) {\n'\
    '               cg[i].style.display = "block";\n'\
    '           } else if(cg[i].style.display != "none") {\n'\
    '               cg[i].style.display = "none";\n'\
    '           }\n'\
    '       }\n'\
    '   }\n'\
    '   window.addEventListener("load", function () {\n'\
    '       var dmesg = document.getElementById("dmesg");\n'\
    '       var dev = dmesg.getElementsByClassName("thread");\n'\
    '       for (var i = 0; i < dev.length; i++) {\n'\
    '           dev[i].onclick = deviceDetail;\n'\
    '       }\n'\
    '   });\n'\
    '</script>\n'
    hf.write(script_code);

# Function: suspendSupported
# Description:
#     Verify that the requested mode is supported
def suspendSupported():
    global sysvals

    if(not os.path.exists(sysvals.powerfile)):
        print("%s doesn't exist", sysvals.powerfile)
        return False

    ret = False
    fp = open(sysvals.powerfile, 'r')
    modes = string.split(fp.read())
    for mode in modes:
        if(mode == sysvals.suspendmode):
            ret = True
    fp.close()
    if(not ret):
        print("ERROR: %s mode not supported") % sysvals.suspendmode
        print("Available modes are: %s") % modes
    else:
        print("Using %s mode for suspend") % sysvals.suspendmode
    return ret

# Function: executeSuspend
# Description:
#     Execute system suspend through the sysfs interface
def executeSuspend():
    global sysvals, data

    pf = open(sysvals.powerfile, 'w')
    # clear the kernel ring buffer just as we start
    os.system("dmesg -C")
    # start ftrace
    if(data.useftrace):
        print("START TRACING")
        os.system("echo 1 > "+sysvals.tpath+"tracing_on")
        os.system("echo SUSPEND START > "+sysvals.tpath+"trace_marker")
    # initiate suspend
    print("SUSPEND START")
    pf.write(sysvals.suspendmode)
    # execution will pause here
    pf.close() 
    # return from suspend
    print("RESUME COMPLETE")
    # stop ftrace
    if(data.useftrace):
        os.system("echo RESUME COMPLETE > "+sysvals.tpath+"trace_marker")
        os.system("echo 0 > "+sysvals.tpath+"tracing_on")
        print("CAPTURING FTRACE")
        os.system("echo \""+sysvals.teststamp+"\" > "+sysvals.ftracefile)
        os.system("cat "+sysvals.tpath+"trace >> "+sysvals.ftracefile)
    # grab a copy of the dmesg output
    print("CAPTURING DMESG")
    os.system("echo \""+sysvals.teststamp+"\" > "+sysvals.dmesgfile)
    os.system("dmesg -c >> "+sysvals.dmesgfile)

def printHelp():
    global sysvals
    modes = ""
    if(os.path.exists(sysvals.powerfile)):
        fp = open(sysvals.powerfile, 'r')
        modes = string.split(fp.read())
        fp.close()

    print("")
    print("AnalyzeSuspend")
    print("Usage: sudo analyze_suspend.py <options>")
    print("")
    print("Description:")
    print("  Initiates a system suspend/resume while capturing dmesg")
    print("  and (optionally) ftrace data to analyze device timing")
    print("")
    print("  Generates output files in subdirectory: suspend-mmddyy-HHMMSS")
    print("    HTML output:                    <hostname>_<mode>.html")
    print("    raw dmesg output:               <hostname>_<mode>_dmesg.txt")
    print("    raw ftrace output (with -f):    <hostname>_<mode>_ftrace.txt")
    print("")
    print("Options:")
    print("    -h                     Print this help text")
    print("    -verbose               Print extra information during execution and analysis")
    print("    -m mode                Mode to initiate for suspend %s (default: %s)") % (modes, sysvals.suspendmode)
    print("    -f                     Use ftrace to create device callgraphs (default: disabled)")
    print("  (Re-analyze data from previous runs)")
    print("    -dmesg  dmesgfile      Create timeline svg from dmesg file")
    print("    -ftrace ftracefile     Create callgraph HTML from ftrace file")
    print("")
    return True

def doError(msg, help):
    print("ERROR: %s") % msg
    if(help == True):
        printHelp()
    sys.exit()

def numCpus():
    val = 2
    fp = open("/proc/cpuinfo", 'r')
    if(fp):
        val = fp.read().count('vendor_id')
    return val

# -- script main --
# loop through the command line arguments
args = iter(sys.argv[1:])
for arg in args:
    if(arg == "-m"):
        try:
            val = args.next()
        except:
            doError("No mode supplied", True)
        sysvals.suspendmode = val
    elif(arg == "-f"):
        data.useftrace = True
    elif(arg == "-verbose"):
        data.verbose = True
    elif(arg == "-dmesg"):
        try:
            val = args.next()
        except:
            doError("No dmesg file supplied", True)
        data.notestrun = True
        data.usedmesg = True
        sysvals.dmesgfile = val
    elif(arg == "-ftrace"):
        try:
            val = args.next()
        except:
            doError("No ftrace file supplied", True)
        data.notestrun = True
        data.useftrace = True
        sysvals.ftracefile = val
    elif(arg == "-h"):
        printHelp()
        sys.exit()
    else:
        doError("Invalid argument: "+arg, True)

data.initialize()

# if instructed, re-analyze existing data files
if(data.notestrun):
    sysvals.setOutputFile()
    data.vprint("Output file: %s" % sysvals.htmlfile)
    if(sysvals.dmesgfile != ""):
        analyzeKernelLog()
    if(sysvals.ftracefile != ""):
        analyzeTraceLog()
    createHTML()
    sys.exit()

# verify that we can run a test
data.usedmesg = True
if(os.environ['USER'] != "root"):
    doError("This script must be run as root", False)
if(not suspendSupported()):
    sys.exit()
if(data.useftrace and not verifyFtrace()):
    sys.exit()

# prepare for the test
if(data.useftrace):
    initFtrace()
sysvals.initTestOutput()

data.vprint("Output files:\n    %s" % sysvals.dmesgfile)
if(data.useftrace):
    data.vprint("    %s" % sysvals.ftracefile)
data.vprint("    %s" % sysvals.htmlfile)

# execute the test
executeSuspend()
analyzeKernelLog()
if(data.useftrace):
    analyzeTraceLog()
createHTML()
