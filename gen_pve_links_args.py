#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re, sys, subprocess
from pathlib import Path
import yaml

ETH_RE = re.compile(r'^(?:eth)?(\d+)$', re.IGNORECASE)
NET_LINE_RE = re.compile(r'^net(\d+):')
PCI_ADDR_RE = re.compile(r',addr=0x([0-9a-fA-F]+)')

def eth_idx(token):
    """Return integer interface index from 'ethX' or 'X'."""
    m = ETH_RE.match(str(token).strip())
    if not m:
        raise SystemExit(f"Invalid eth index: {token}")
    return int(m.group(1))

def parse_mac_prefix(pref: str):
    """Parse a 3-byte MAC prefix (e.g. 'bc:24:99'). Return tuple of ints."""
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
    # Ensure locally administered bit set to avoid OUI collisions
    vals[0] |= 0x02
    vals[0] &= 0xfe  # clear multicast bit
    return tuple(vals)

def gen_mac(mac_prefix, vmid: int, iface_index: int) -> str:
    """Generate deterministic MAC from 3-byte prefix + vmid (2 bytes) + iface (1 byte)."""
    b1, b2, b3 = mac_prefix
    return f"{b1:02x}:{b2:02x}:{b3:02x}:{(vmid >> 8) & 0xff:02x}:{vmid & 0xff:02x}:{iface_index & 0xff:02x}"

def load_cfg(path: Path):
    """Load YAML mapping file."""
    d = yaml.safe_load(path.read_text())
    if not isinstance(d, dict):
        raise SystemExit("Top-level YAML must be a mapping.")
    
    defs = d.get("defaults", {})
    links = d.get("links", [])
    if not links:
        raise SystemExit("No 'links' provided.")

    # Parse links
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
        "udp_port_base": int(defs.get("udp_port_base", 40000)),
        "udp_map": {int(k): v for k, v in (defs.get("udp_ip_by_vm", {})).items()},
        "udp_default_ip": str(defs.get("udp_default_ip", "127.0.0.1")),
        "loopback_if_same_host": bool(defs.get("loopback_if_same_host", True)),
        "links": parsed,
    }

def highest_existing_net_index(vmid: int) -> int:
    """Parse /etc/pve/qemu-server/<vmid>.conf and return highest existing netN index."""
    conf_path = Path(f"/etc/pve/qemu-server/{vmid}.conf")
    if not conf_path.exists():
        return -1
    try:
        indices = [int(NET_LINE_RE.match(line.strip()).group(1)) 
                   for line in conf_path.read_text().splitlines() 
                   if NET_LINE_RE.match(line.strip())]
        return max(indices, default=-1)
    except Exception:
        return -1

def highest_existing_pci_addr(vmid: int) -> int:
    """Run `qm showcmd` and parse output to find highest existing PCI address for a netdev."""
    default_addr = 0x0F
    try:
        result = subprocess.run(["qm", "showcmd", str(vmid)], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return default_addr
        
        args = re.findall(r"(?:[^\s\"']+|\"[^\"]*\"|'[^']*')", result.stdout)
        addresses = [int(PCI_ADDR_RE.search(arg).group(1), 16) 
                     for arg in args 
                     if 'netdev=' in arg and PCI_ADDR_RE.search(arg)]
        return max(addresses, default=default_addr)
    except Exception:
        return default_addr

def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} mapping.yaml", file=sys.stderr)
        sys.exit(1)
    
    cfg = load_cfg(Path(sys.argv[1]))
    
    # Extract configuration
    prefix_digit = str(cfg["udp_port_base"])[0]
    
    # Count additional NICs per VM
    add_counts = {}
    for (A, Ai), (B, Bi) in cfg["links"]:
        add_counts[A] = add_counts.get(A, 0) + 1
        add_counts[B] = add_counts.get(B, 0) + 1
    
    # Initialize indices and addresses
    current_index = {vm: highest_existing_net_index(vm) + 1 for vm in add_counts}
    current_addr = {vm: highest_existing_pci_addr(vm) + 1 for vm in add_counts}
    start_index = current_index.copy()
    
    per_vm_args = {}
    used_ports = {}
    
    def alloc_port(vmid, eth_idx):
        if vmid > 999 or eth_idx > 9:
            raise SystemExit(f"VMID {vmid} must be <= 999 and eth index {eth_idx} must be <= 9")
        port_val = int(f"{prefix_digit}{vmid}{eth_idx}")
        if port_val > 65535:
            raise SystemExit(f"Port {port_val} out of range")
        if port_val in used_ports and used_ports[port_val] != (vmid, eth_idx):
            prev = used_ports[port_val]
            raise SystemExit(f"Port collision {port_val} between VM{vmid} eth{eth_idx} and VM{prev[0]} eth{prev[1]}")
        used_ports[port_val] = (vmid, eth_idx)
        return port_val
    
    # Generate network arguments for each link
    for (A, Ai), (B, Bi) in cfg["links"]:
        ipA = cfg["udp_map"].get(A, cfg["udp_default_ip"])
        ipB = cfg["udp_map"].get(B, cfg["udp_default_ip"])
        
        # Use loopback if both VMs on same host
        if cfg["loopback_if_same_host"] and ipA == ipB and ipA not in ("127.0.0.1", "::1"):
            ipA = ipB = "127.0.0.1"
        
        # Generate MACs and allocate resources
        macA = gen_mac(cfg["mac_prefix"], A, Ai)
        macB = gen_mac(cfg["mac_prefix"], B, Bi)
        
        idxA, idxB = current_index[A], current_index[B]
        current_index[A] += 1
        current_index[B] += 1
        
        addrA, addrB = current_addr[A], current_addr[B]
        current_addr[A] += 1
        current_addr[B] += 1
        
        portA, portB = alloc_port(A, Ai), alloc_port(B, Bi)
        
        # Build QEMU arguments
        for vm, idx, addr, mac, local_ip, local_port, remote_ip, remote_port in [
            (A, idxA, addrA, macA, ipA, portA, ipB, portB),
            (B, idxB, addrB, macB, ipB, portB, ipA, portA)
        ]:
            netdev = f"-netdev socket,id=net{idx},udp={remote_ip}:{remote_port},localaddr={local_ip}:{local_port}"
            device = f"-device {cfg['model']},mac={mac},rx_queue_size={cfg['rxq']},tx_queue_size={cfg['txq']},netdev=net{idx},id=net{idx},bus=pci.0,addr=0x{addr:x}"
            if cfg["host_mtu"] > 0:
                device += f",host_mtu={cfg['host_mtu']}"
            per_vm_args.setdefault(vm, []).extend([netdev, device])
    
    # Output results
    if used_ports:
        print(f"# UDP ports used: {', '.join(str(p) for p in sorted(set(used_ports.keys())))} (formula: {prefix_digit} + VMID + ethIndex)")
    print("# Existing netN count per VM respected (starting indices):")
    for vm in sorted(start_index):
        print(f"#   VM {vm}: starting at net{start_index[vm]}")
    print("")
    
    for vm in sorted(per_vm_args.keys()):
        args = " ".join(per_vm_args[vm])
        print(f"# VM {vm}")
        print(f"qm set {vm} --args \"{args}\"\n")

if __name__ == "__main__":
    main()
