#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re, sys, subprocess
from pathlib import Path
import yaml

ETH_RE = re.compile(r'^(?:eth)?(\d+)$', re.IGNORECASE)
NET_LINE_RE = re.compile(r'^net(\d+):')
PCI_ADDR_RE = re.compile(r',addr=0x([0-9a-fA-F]+)(?:\.0x[0-9a-fA-F]+)?')
NETDEV_PCI_ADDR_RE = re.compile(r"virtio-net-pci[^']*addr=0x([0-9a-fA-F]+)", re.IGNORECASE)
NETDEV_FULL_RE = re.compile(
    r"virtio-net-pci[^']*mac=([0-9a-fA-F:]+)[^']*netdev=net(\d+)[^']*addr=0x([0-9a-fA-F]+)",
    re.IGNORECASE
)

def eth_idx(token):
    m = ETH_RE.match(str(token).strip())
    if not m:
        raise SystemExit(f"Invalid eth index: {token}")
    return int(m.group(1))

def parse_mac_prefix(pref: str):
    parts = str(pref).lower().split(':')
    if len(parts) != 3:
        raise SystemExit(f"mac_prefix must have 3 bytes (got {pref})")
    try:
        vals = [int(p, 16) for p in parts]
    except ValueError:
        raise SystemExit(f"mac_prefix contains non-hex bytes: {pref}")
    for v in vals:
        if not (0 <= v <= 255):
            raise SystemExit(f"mac_prefix byte out of range: {pref}")
    vals[0] |= 0x02      # locally administered
    vals[0] &= 0xfe      # clear multicast bit
    return tuple(vals)

def gen_mac(mac_prefix, vmid: int, iface_index: int) -> str:
    b1, b2, b3 = mac_prefix
    return f"{b1:02x}:{b2:02x}:{b3:02x}:{(vmid >> 8) & 0xff:02x}:{vmid & 0xff:02x}:{iface_index & 0xff:02x}"

def load_cfg(path: Path):
    d = yaml.safe_load(path.read_text())
    if not isinstance(d, dict):
        raise SystemExit("Top-level YAML must be a mapping.")
    defs = d.get("defaults", {})
    links = d.get("links", [])
    if not links:
        raise SystemExit("No 'links' provided.")
    parsed = []
    for item in links:
        if not isinstance(item, (list, tuple)) or len(item) != 4:
            raise SystemExit(f"Each link must be [vmA, ethA|A, vmB, ethB|B], got: {item}")
        parsed.append(((int(item[0]), eth_idx(item[1])), (int(item[2]), eth_idx(item[3]))))
    return {
        "model": str(defs.get("model", "virtio-net-pci")),
        "host_mtu": int(defs.get("host_mtu", 9300)),
        "rxq": int(defs.get("rx_queue_size", 1024)),
        "txq": int(defs.get("tx_queue_size", 256)),
        "mac_prefix": parse_mac_prefix(defs.get("mac_prefix", "bc:24:99")),
        "udp_port_base": int(defs.get("udp_port_base", 40000)),  # only first digit used below
        "udp_map": {int(k): v for k, v in (defs.get("udp_ip_by_vm", {})).items()},
        "udp_default_ip": str(defs.get("udp_default_ip", "127.0.0.1")),
        "loopback_if_same_host": bool(defs.get("loopback_if_same_host", True)),
        "auto_pci_addr": bool(defs.get("auto_pci_addr", False)),
        "pci_alloc_strategy": str(defs.get("pci_alloc_strategy", "lowest_free")).lower(),
        "links": parsed,
    }

def highest_existing_net_index(vmid: int) -> int:
    conf_path = Path(f"/etc/pve/qemu-server/{vmid}.conf")
    if not conf_path.exists():
        return -1
    try:
        hi = -1
        for line in conf_path.read_text().splitlines():
            m = NET_LINE_RE.match(line.strip())
            if m:
                idx = int(m.group(1))
                if idx > hi:
                    hi = idx
        return hi
    except Exception:
        return -1

def pci_info(vmid: int):
    used = set()
    highest = -1
    try:
        res = subprocess.run(["qm", "showcmd", str(vmid)], capture_output=True, text=True, check=False)
        if res.returncode != 0:
            return used, highest
        out = res.stdout
        for m in PCI_ADDR_RE.finditer(out):
            try:
                used.add(int(m.group(1), 16))
            except ValueError:
                pass
        for m in NETDEV_PCI_ADDR_RE.finditer(out):
            try:
                val = int(m.group(1), 16)
                if val > highest:
                    highest = val
            except ValueError:
                pass
    except Exception:
        pass
    return used, highest

def collect_existing_nics(vmid: int):
    mapping = {}
    try:
        res = subprocess.run(["qm", "showcmd", str(vmid)], capture_output=True, text=True, check=False)
        if res.returncode != 0:
            return mapping
        for m in NETDEV_FULL_RE.finditer(res.stdout):
            mac = m.group(1).lower()
            net_idx = int(m.group(2))
            pci_addr = int(m.group(3), 16)
            mapping[mac] = (net_idx, pci_addr)
    except Exception:
        pass
    return mapping

def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} mapping.yaml", file=sys.stderr)
        sys.exit(1)
    cfg = load_cfg(Path(sys.argv[1]))
    pci_strategy = cfg["pci_alloc_strategy"]
    if pci_strategy not in ("lowest_free", "next_highest"):
        raise SystemExit(f"Invalid pci_alloc_strategy '{pci_strategy}' (use lowest_free or next_highest)")
    prefix_digit = str(cfg["udp_port_base"])[0]

    # Count scheduled NIC additions per VM
    add_counts = {}
    for (A, _Ai), (B, _Bi) in cfg["links"]:
        add_counts[A] = add_counts.get(A, 0) + 1
        add_counts[B] = add_counts.get(B, 0) + 1

    # Gather existing NICs per VM (for reuse)
    existing_nics_by_vm = {vm: collect_existing_nics(vm) for vm in add_counts}

    # Starting net indices (only used for NEW interfaces)
    current_index = {vm: highest_existing_net_index(vm) + 1 for vm in add_counts}
    start_index = current_index.copy()

    auto_pci_enabled = cfg["auto_pci_addr"]
    if not auto_pci_enabled:
        pci_used = {}
        highest_nic_slot = {}
        for vm in add_counts:
            used_slots_set, hi_slot = pci_info(vm)
            pci_used[vm] = used_slots_set
            highest_nic_slot[vm] = hi_slot
        # Reserve bridge slots
        for vm in pci_used:
            pci_used[vm].update({0x1e, 0x1f})
        def alloc_pci(vm):
            # next_highest strategy; fallback to lowest_free if would exceed usable range
            if pci_strategy == "next_highest":
                start = max(highest_nic_slot[vm] + 1, 0x02)
                if start < 0x1e:  # can place at or above 0x02, below 0x1e
                    for addr in range(start, 0x1e):
                        if addr not in pci_used[vm]:
                            pci_used[vm].add(addr)
                            highest_nic_slot[vm] = max(highest_nic_slot[vm], addr)
                            return addr
            # lowest_free (or fallback)
            for addr in range(0x02, 0x1e):
                if addr not in pci_used[vm]:
                    pci_used[vm].add(addr)
                    if addr > highest_nic_slot[vm]:
                        highest_nic_slot[vm] = addr
                    return addr
            raise SystemExit(f"PCI slots 0x02-0x1d exhausted for VM {vm}; set auto_pci_addr:true or free a slot.")
    else:
        alloc_pci = None  # unused

    per_vm_args = {}
    used_ports = {}
    reused_ifaces = []  # (vm, eth_idx, net_idx, pci_addr)

    def alloc_port(vmid, eth_index):
        if vmid > 999 or eth_index > 9:
            raise SystemExit(f"VMID {vmid} must be <=999 and eth index {eth_index} <=9")
        port = int(f"{prefix_digit}{vmid}{eth_index}")
        if port > 65535:
            raise SystemExit(f"Port {port} out of range")
        prev = used_ports.get(port)
        if prev and prev != (vmid, eth_index):
            raise SystemExit(f"Port collision {port} between VM{vmid} eth{eth_index} and VM{prev[0]} eth{prev[1]}")
        used_ports[port] = (vmid, eth_index)
        return port

    # Build args
    seen_vm_iface = set()
    for (A, Ai), (B, Bi) in cfg["links"]:
        ipA = cfg["udp_map"].get(A, cfg["udp_default_ip"])
        ipB = cfg["udp_map"].get(B, cfg["udp_default_ip"])
        if cfg["loopback_if_same_host"] and ipA == ipB and ipA not in ("127.0.0.1", "::1"):
            ipA = ipB = "127.0.0.1"

        macA = gen_mac(cfg["mac_prefix"], A, Ai).lower()
        macB = gen_mac(cfg["mac_prefix"], B, Bi).lower()

        portA = alloc_port(A, Ai)
        portB = alloc_port(B, Bi)

        for vm, eth_idx, mac, local_ip, local_port, remote_ip, remote_port in [
            (A, Ai, macA, ipA, portA, ipB, portB),
            (B, Bi, macB, ipB, portB, ipA, portA)
        ]:
            if (vm, eth_idx) in seen_vm_iface:
                raise SystemExit(f"Duplicate interface index eth{eth_idx} for VM {vm} in links.")
            seen_vm_iface.add((vm, eth_idx))

            # Reuse existing NIC if MAC present
            existing = existing_nics_by_vm.get(vm, {})
            reuse_idx = reuse_pci = None
            if mac in existing:
                reuse_idx, reuse_pci = existing[mac]

            if reuse_idx is not None:
                idx = reuse_idx
            else:
                idx = current_index[vm]
                current_index[vm] += 1

            netdev = f"-netdev socket,id=net{idx},udp={remote_ip}:{remote_port},localaddr={local_ip}:{local_port}"

            if not auto_pci_enabled:
                if reuse_pci is not None:
                    if reuse_pci not in pci_used[vm]:
                        pci_used[vm].add(reuse_pci)
                    parts = [
                        f"-device {cfg['model']}",
                        f"mac={mac}",
                        f"netdev=net{idx}",
                        "bus=pci.0",
                        f"addr=0x{reuse_pci:x}",
                        f"id=net{idx}",
                        f"rx_queue_size={cfg['rxq']}",
                        f"tx_queue_size={cfg['txq']}",
                    ]
                    reused_ifaces.append((vm, eth_idx, idx, reuse_pci))
                else:
                    addr = alloc_pci(vm)
                    parts = [
                        f"-device {cfg['model']}",
                        f"mac={mac}",
                        f"netdev=net{idx}",
                        "bus=pci.0",
                        f"addr=0x{addr:x}",
                        f"id=net{idx}",
                        f"rx_queue_size={cfg['rxq']}",
                        f"tx_queue_size={cfg['txq']}",
                    ]
            else:
                parts = [
                    f"-device {cfg['model']}",
                    f"mac={mac}",
                    f"netdev=net{idx}",
                    f"id=net{idx}",
                    f"rx_queue_size={cfg['rxq']}",
                    f"tx_queue_size={cfg['txq']}",
                ]

            if cfg["host_mtu"] > 0:
                parts.append(f"host_mtu={cfg['host_mtu']}")
            device = ",".join(parts)
            per_vm_args.setdefault(vm, []).extend([netdev, device])

    if used_ports:
        print(f"# UDP ports used: {', '.join(str(p) for p in sorted(used_ports))} (formula: {prefix_digit}+VMID+ethIndex)")
    if reused_ifaces:
        reused_str = ", ".join(f"VM{vm}:eth{e}->net{n}@0x{pa:x}" for vm, e, n, pa in reused_ifaces)
        print(f"# Reused existing NICs (kept PCI addr): {reused_str}")
    print("# Existing netN count per VM respected (starting indices for NEW):")
    for vm in sorted(start_index):
        print(f"#   VM {vm}: starting new at net{start_index[vm]}")
    print("")
    for vm in sorted(per_vm_args):
        print(f"# VM {vm}")
        print(f"qm set {vm} --args \"{' '.join(per_vm_args[vm])}\"")

if __name__ == "__main__":
    main()
