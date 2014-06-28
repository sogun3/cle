#!/usr/bin/env python

from ctypes import *
import os
import logging
import subprocess
import platform
import binascii

l = logging.getLogger("cle")

class CLException(Exception):
    def __init__(self, val):
        self.val = val

    def __str__(self):
        return repr(self.val)


class Segment(object):
    """ Simple representation of an ELF file segment"""
    def __init__(self, name, vaddr, size, offset=None):
        self.vaddr = vaddr
        self.size = size
        self.offset = offset
        self.name = name

    def contains_addr(self, addr):
            return ((addr > self.vaddr) and (addr < self.vaddr + self.size))


class Elf(object):
    """ Representation of loaded Elf binaries """
    def __init__(self, binary):

        self.segments = [] # List of segments
        self.memory = {} # Private virtual address space, without relocations
        self.symbols = {} # Object's symbols
        self.rebase_addr = 0
        self.object_type = None
        self.entry_point = None
        self.deps = None # Needed shared objects (libraries dependencies)

        if (os.path.exists(binary)):
            self.binary = binary
        else:
            raise CLException("The binary file \"%s\" does not exist :(" %
                              binary)

        l.debug(" [Loading binary object %s]" % self.binary)
        arch_name = self.__get_bfd_arch(binary)

        # We use qemu's convention for arch names
        self.arch = self.arch_to_qemu_arch(arch_name)
        info = self.__call_clextract(binary)
        self.symbols = self.__get_symbols(info)
        self.entry_point = self.__get_entry_point(info)
        self.phdr = self.__get_phdr(info)
        self.deps = self.__get_lib_names(info)
        self.dynamic = self.__get_dynamic(info)
        self.jmprel = self.__get_jmprel(info)
        self.endianness = self.__get_endianness(info)
        self.load()



    def __get_exec_base_addr(self):
        """
        Return the virtual address of the segment that has the lowest address.
        This is only relevant to executable files, as shared libraries would
        have 0 as their text segment load addresses """

        t = self.get_text_phdr_ent()
        d = self.get_data_phdr_ent()

        if t["vaddr"] > d["vaddr"]:
            return d["vaddr"]
        else:
            return t["vaddr"]



    def __get_max_addr(self):
        """ This returns the highest virtual address contained in any loaded
        segment of the binary

        NOTE: relocation is taken into consideration, if it exists. By default,
        rebase_addr is zero.
        When this is called by Clé's loader (Ld), relocations are already in place.
        When this function is called directly, the behavior w.r.t relocation is
        undefined, and depends on whether the caller set rebase_addr to any
        value.
        """

        text = self.get_text_phdr_ent()
        data = self.get_data_phdr_ent()

        m1 = text["vaddr"] + text["memsz"] + self.rebase_addr
        m2 = data["vaddr"] + data["memsz"] + self.rebase_addr

        if m1 > m2:
            return m1
        return m2


    def __get_phdr(self, data):
        """ Get program header table """
        phdr = []
        int_fields = ["offset", "vaddr", "filesz", "memsz", "align"]

        for d in data:
            # Create a new dictionary for each program header of the table
            if d[0] == "phdr":
                idx = 1
                h = {}
                # Add integer fields
                for f in int_fields:
                    h[f] = int(d[idx])
                    idx += 1
                # Type is a string
                h["type"] = d[idx].strip()
                phdr.append(h)
        return phdr



    def __get_shdr(self, data):
        """ Get section header table if present """
        shdr = []
        for i in data:
            # Program headers
            if i[0] == "shdr":
                shdr.append(i)
        return shdr


    def __get_dynamic(self, data):
        """ Get the dynamic section """
        dyn = []
        for i in data:
            if i[0] == "dyn":
                dyn.append(i)
        return dyn


    def __get_entry_point(self, data):
        """ Get entry point """
        for i in data:
            if i[0] == "Entry point":
                return i[1]



    def __get_endianness(self, data):
        for i in data:
            if i[0] == "Endianness":
                return i[1]


    def get_object_type(self, data):
        """ Get ELF type """
        for i in data:
            if i[0] == "Object_type":
                return i[1]



    def __get_symbols(self, data):
        """ Get symbols addresses """
        symbols = {}
        symb = self.__symb(data)
        for i in symb:
            s = {}
            name = i[9].strip()
            s["addr"] = int(i[2].strip())
            s["binding"] = i[5].strip()
            s["type"] = i[8].strip()
            symbols[name] = s

        return symbols



    def __symb(self, data):
        """ Extract symbol table entries from Clextract"""
        symb = []
        for i in data:
            # Symbols table
            if i[0] == "symtab":
                symb.append(i)
        return symb



    def __get_jmprel(self, data):
        """ Get the location of the GOT slots corresponding to the addresses of
        relocated symbols (jump targets of the (PLT)"""
        got = {}
        for i in data:
            if i[0].strip() == "jmprel":
                # See the output of clextract:
                # i[3] is the symbol name, i[1] is the GOT location
                got[i[3].strip()] = int(i[1].strip())
        return got



    def get_text_phdr_ent(self):
        """ Return the entry of the program header table corresponding to the
        text segment"""
        for i in self.phdr:
            if i["type"] == "PT_LOAD" and i["filesz"] == i["memsz"]:
                return i


    def get_data_phdr_ent(self):
        """ Return the enty of the program header table corresponding to the
        data segment"""
        for i in self.phdr:
            # The data segment is smaller in the file than in memory because of
            # the BSS section (not represented in the file as it only contains
            # null bytes)
            if (i["type"] == "PT_LOAD") and (i["filesz"] != i["memsz"]):
                return i



    def get_imports(self):
        """ Get imports from symbol table """
        imports = {}
        for name,properties in self.symbols.iteritems():
        # Imports are symbols with type SHN_UNDEF in the symbol table.
            addr = properties["addr"]
            s_type = properties["type"]
            if (s_type == "SHN_UNDEF"):
                imports[name] = int(addr)
        return imports



    def get_exports(self):
        """ We can basically say that any symbol defined with an address is a
        potential export """
        exports = {}
        for name,prop in self.symbols.iteritems():
            addr = prop["addr"]
            binding = prop["binding"]

            # Exports have STB_GLOBAL binding propertie. TODO: STB_WEAK ?
            if (binding == "STB_GLOBAL"):
                exports[name] = addr
        return exports



    def __get_bfd_arch(self, binary):
        """ Get the architecture name using ctypes and cle_bfd.so """
        env_p = os.getenv("VIRTUAL_ENV")
        lib_p = "lib"
        lib = os.path.join(env_p, lib_p, "cle_bfd.so")
        if os.path.exists(lib):
            self.lib = cdll.LoadLibrary(lib)
            self.lib.get_bfd_arch.restype = c_char_p
            return self.lib.get_bfd_arch(binary)
        else:
            raise CLException("Cannot load cle_bfd.so, invalid path:%s" % lib)



    def __get_lib_names(self, data):
        """ What are the dependencies of the binary ?
        This gets the names of the libraries we should load as well, from the
        dynamic segment """
        deps = []
        for i in data:
            # The first index is the string "needed"
            if i[0] == "needed":
                # The other idexes are the actual dependencies
                for dep in i[1:]:
                    deps.append(dep.strip()) # Remove extra spaces

        l.debug("\t--> binary depends on %s" % repr(deps))
        return deps


    def __call_clextract(self, binary):
        """ Get information from the binary using clextract """
        qemu = self.get_qemu_cmd()
        arch = self.arch
        env_p = os.getenv("VIRTUAL_ENV")
        bin_p = "opt/%s" % arch
        cle = os.path.join(env_p, bin_p, "clextract")
        if (not os.path.exists(cle)):
            raise CLException("Cannot find clextract binary at %s" % cle)

        # clextract needs libcle which resides in arch/ for each arch
        cmd = [qemu, "-E", "LD_LIBRARY_PATH=" + os.path.join(env_p, bin_p), cle,
               self.binary]
        s = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        out = s.communicate()
        err = s.returncode

        # We want to make sure qemu returns correctly before we interpret
        # the output. TODO: we should also get clextract's return code (maybe
        # through an ENV variable ?)
        if (err != 0):
            raise CLException("Qemu returned error %d while running %s :("
                              % (err, repr(cmd)))

        else:

            # For some reason all the relevant output resides in out[0]
            data = out[0].splitlines()

            # All the fields are separated by commas, see clextract.c
            s = []
            for i in data:
                s.append(i.split(","))
            return s



    def get_qemu_cmd(self):
        """ Find the right qemu-{cmd} for the binary's architecture """
        cmd = "qemu-%s" % self.arch

        # Check if the command actually exists on the system
        s = subprocess.Popen(["which", cmd], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        out = s.communicate()
        err = s.returncode

        # Which returns 0 if the command exists
        if (err != 0):
            raise CLException("Cannot find \"%s\", it does not exist or is not"
                              " in PATH :: %s" % (cmd, out))
        else:
            return cmd



    def arch_to_qemu_arch(self, arch):
        """ We internally use the BFD architecture names.
         This converts names to the convension used by qemu-user to name its
         different qemu-{arch} architectures. """

        if arch == "i386:x86-64":
            return "x86_64"
        elif arch == "mips:isa32":
            return "mips"
        elif arch == "powerpc:common":
            return "ppc"
        elif arch == "armv4t":
            return "arm"
        elif arch == "i386":
            return "i386"

        else:
            raise CLException("Architecture name conversion not implemented yet"
                              "for \"%s\" !" % arch)


    def arch_to_simuvex_arch(self, arch):
        """ This function translates architecture names from the BFD convention
        to the convention used by simuvex """

        if arch == "i386:x86-64":
            return "AMD64"
        elif arch == "mips:isa32":
            return "MIPS32"
        elif arch == "powerpc:common":
            return "PPC32"
        elif arch == "armv4t":
            return "ARM"
        elif arch == "i386":
            return "X86"


    def load(self):
        """ Load the binary file @binary into memory"""

        text = self.get_text_phdr_ent()
        data = self.get_data_phdr_ent()
        self.__load(text, "text")
        self.__load(data, "data")

        # The data segment is also supposed to contain the BSS section
        self.__load_bss(data)



    def __load_bss(self, data_hdr):
        """ The BSS section does not appear in the binary file, but its size is
        the difference between the binary size and the process memory image size
        """
        size = data_hdr["memsz"] - data_hdr["filesz"]
        off = data_hdr["vaddr"] + data_hdr["filesz"]
        for i in range(off, off + size):
            self.memory[i] = 0x0




    def __load(self, hdrinfo, name):
        """ Stub to load the text segment """
        if not hdrinfo:
            raise CLException("No program header entry for the %s segment was"
                               " found :(" % name)
        self.load_segment(hdrinfo["offset"], hdrinfo["filesz"],
                          hdrinfo["vaddr"], name)



    def contains_addr(self, addr):
        """ Is @vaddr in one of the binary's segments we have loaded ?
        (i.e., is it mapped into memory ?)
        """
        for i in self.segments:
            if contains_addr(i, addr):
                return True
        return False


    def in_which_segment(self, vaddr):
        """ What is the segment name containing @vaddr ?"""
        for s in self.segments:
            if s.contains_addr(vaddr):
                return s.name
        return None


    def load_segment(self, offset, size, vaddr, name=None):
        """ Load a segment into memory """

        try:
            f = open(self.binary, 'r')
            f.seek(offset)
        except IOError:
            print("\tFile does not exist", self.binary)

        # Fill the memory dict with addr:value
        for i in range(vaddr, vaddr + size):
            # Is something else already loaded at this address ?
            if i in self.memory:
                raise CLException("WTF?? @0x%x Segments overlaping in memory",
                                  i)
            self.memory[i] = f.read(1)

        # Add the segment to the list of loaded segments
        seg = Segment(name, vaddr, size)
        self.segments.append(seg)
        l.debug("\t--> Loaded segment %s @0x%x with size:0x%x" % (name, vaddr,
                                                                size))


class Ld(object):
    """ CLE ELF loader
    The loader loads all the objects and exports an abstraction of the memory of
    the process.
    """

    def __init__(self, binary):
        """ @path is the path to licle_ctypes.so"""

        self.memory = {} # Dictionary representation of the memory
        self.shared_objects =[] # Executables and libraries
        self.path = binary
        self.main_bin = Elf(binary)
        self.__load_exe()
        self.__load_shared_libs()
        self.__perform_reloc()


    def host_endianness(self):
        if (sys.byteorder == "little"):
            return "LSB"
        else:
            return "MSB"


    def __perform_reloc(self):
        # Main binary
        self.__reloc(self.main_bin)

        # Libraries
        for obj in self.shared_objects:
            self.__reloc(obj)



    def addr_belongs_to_object(self, addr):
        max = self.main_bin.__get_max_addr()
        min = self.main_bin.__get_exec_base_addr()

        if (addr > min and addr < max):
            return self.main_bin

        for so in self.shared_objects:
            max = so.__get_max_addr()
            min = so.rebase_addr
            if (addr > min and addr < max):
                return so



    def min_addr(self):
        """ The minimum base address of any loaded object """

        # Let's start with the main executable
        base = self.main_bin.__get_exec_base_addr(self)

        # Libraries usually have 0 as their base address, until relocation.
        # It is unlikely that libraries get relocated at a lower address than
        # the main binary, but we never know...
        for i in self.shared_objects:
            if (i.rebase_addr > 0 and i.rebase_addr < base):
                base = i.rebase_addr

        return base



    def max_addr(self):
        """ The maximum address loaded as part of any loaded object """

        m1 = self.main_bin.__get_max_addr()
        for i in self.shared_objects:
            m2 = i.__get_max_addr()
            if m2 > m1:
                m1 = m2
        return m1



    def __reloc(self, obj):
        # Now let's update GOT entries for PLT jumps
        l.debug(" [Performing relocations of %s]" % obj.binary)
        for symb, got_addr in obj.jmprel.iteritems():
            #s_type = obj.symbols[symb]["type"]
           # if (s_type != "SHN_UNDEF"):
           #     l.debug("\t--> skipping relocation of \"%s\"" % symb)
           #     continue
            uaddr = self.find_symbol_addr(symb)
            if (uaddr):
                uaddr = uaddr + obj.rebase_addr
                l.debug("\t--> Relocation of %s -> 0x%x" %
                        (symb, int(uaddr)))
                self.memory[got_addr] = uaddr
            else:
                l.debug("\t--> Cannot locate symbol \"%s\" from SOs" % symb)



    def override_got_entry(self, name, newaddr, obj):
        """ This overrides the address of the function defined by @symbol with
        the new address @newaddr, inside the GOT of object @obj.
        This is used to call simprocedures instead of actual code """

        got = obj.jmprel

        if not (name in got.keys()):
            l.debug("Could not override the address of symbol %s: symbol not "
                    "found" % name)
            return False

        addr = got[name]
        mem[addr] = newaddr
        return True




    def find_symbol_addr(self, symbol):
        """ Try to get a symbol's address from the exports of shared objects """
        for so in self.shared_objects:
            ex = so.get_exports()
            if symbol in ex:
                return int(ex[symbol]) + so.rebase_addr



    def __load_exe(self):
        """ Load exe into "main memory"""
        for addr, val in self.main_bin.memory.iteritems():
            # There shouldn't be anything in this memory location yet
            if addr in self.memory:
                raise CLException("Something is already loaded at 0x%x" % addr)
            else:
                self.memory[addr] = val



    def __load_shared_libs(self):
        """ Stub to load and rebase shared objects """
        # shared_libs = self.main_bin.deps
        shared_libs = self.ld_so_addr()
        for name, addr in shared_libs.iteritems():
            so = self.__load_so(name)
            self.rebase_lib(so, addr)
            so.rebase_addr = addr
            self.shared_objects.append(so)


    def rebase_lib(self, so, base):
        """ Relocate a shared objet given a base address
        We actually copy the local memory of the object at the new computed
        address in the "main memory" """
        l.debug("\t--> rebasing the binary object @0x%x" %base)
        for addr, data in so.memory.iteritems():
            newaddr = int(addr) + int(base)
            #l.debug("Adding %s at 0x%x" % (repr(data), newaddr))
            self.memory[newaddr] = data


    def ld_so_addr(self):
        """ Use LD_AUDIT to find object dependencies and relocation addresses"""

        qemu = self.main_bin.get_qemu_cmd()
        env_p = os.getenv("VIRTUAL_ENV")
        bin_p = os.path.join(env_p, "opt" ,self.main_bin.arch)

        # Our LD_AUDIT shared object
        ld_audit_obj = os.path.join(bin_p, "ld_audit.so")

        #LD_LIBRARY_PATH
        ld_path = os.getenv("LD_LIBRARY_PATH")
        if ld_path ==None:
            ld_path = bin_p
        else:
            ld_path = ld_path + ":" + bin_p
        var = "LD_LIBRARY_PATH=%s,LD_AUDIT=%s" % (ld_path, ld_audit_obj)

        #LD_AUDIT's output
        log = "./ld_audit.out"

        cmd = [qemu, "-E", var, self.path]
        s = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        s.communicate()

        # Our LD_AUDIT library is supposed to generate a log file.
        # If not we're in trouble
        if (os.path.exists(log)):
            libs = {}
            f = open(log,'r')
            for i in f.readlines():
                lib = i.split(",")
                if lib[0] == "LIB":
                    libs[lib[1]] = int(lib[2].strip(), 16)
            f.close()
            l.debug("---")
            for o,a in libs.iteritems():
                l.debug(" (Will load %s @ 0x%x)" % (o,a) )

            l.debug("---")
            os.remove(log)
            return libs

        else:
            raise CLException("Could not find library dependencies using ld."
                " The log file '%s' does not exist, did qemu fail ? Try to run "
                              "`%s` manually to check" % (log, " ".join(cmd)))


    def __load_so(self, soname):
        """ Load a shared object into memory """
        # Soname can be a path or just the name if the library, in which case we
        # search for it in known paths.
        if (not os.path.exists(soname)):
            path = self.__search_so(soname)
            if (path == None):
                l.debug("\txxx Could not find shared object %s :(" %
                        repr(soname))
                return
        else:
            so = Elf(soname)
            return so



    def __search_so(self, soname):
        """ Looks for a shared object given its filename"""

        # Normally we should not need this as LD knows everything already. But
        # in case we need to look for stuff manually...
        loc = []
        loc.append(os.getenv("LD_LIBRARY_PATH"))
        loc.append(os.path.dirname(self.path))

        for ld_path in loc:
            if not ld_path: continue
            for s_path, s_dir, s_file in os.walk(ld_path):
                sopath = os.path.join(s_path,soname)
                #l.debug("\t--> Trying %s" % sopath)
                if os.path.exists(sopath):
                    l.debug("\t-->Found %s" % sopath)
                    return sopath
        return None


