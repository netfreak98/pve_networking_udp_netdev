# PVE UDP Network Link Generator

The **PVE UDP Network Link Generator** creates **Proxmox VE (PVE) QEMU arguments** for building direct, UDP-based network links between virtual machines.  

Unlike Linux bridges or Open vSwitch, which may suppress or alter certain Layer-2 frames, this method establishes a raw “virtual cable” between VM NICs using QEMU’s UDP socket backend.

---

## Why UDP Links?

Standard virtual networking (Linux bridges, OVS) works well for most workloads but can interfere with or drop less common Layer-2 protocols. In lab or testing environments, it is often necessary to pass these protocols transparently between VMs.

Examples include:

- **Link discovery / negotiation** – LLDP  
- **Interior routing protocols** – IS-IS (ISO)  
- **Link encryption / authentication** – MACsec  
- **Link aggregation** – LACP  

By using UDP sockets as direct pipes between VM interfaces, you can emulate dedicated physical cabling inside Proxmox and accurately test these protocols.
Unlike memory- or file-system- sockets it even allows to connect VMs running on different KVM hypervisors like this.

## Overview

This tool automates the creation of point-to-point network connections between Proxmox VMs using UDP sockets. It generates the necessary QEMU command-line arguments to establish these links without requiring traditional bridges or VLANs.

## Features

- **Direct VM-to-VM networking** using UDP sockets
- **Deterministic MAC address generation** (locally administered, based on VMID + eth index)
- **Idempotent / Re-run safe**: existing NICs (matched by deterministic MAC) are reused (same netN index & PCI addr)
- **Multi-host support** with configurable IP mappings
- **Automatic port allocation** using formula: `<prefixDigit><VMID><ethIndex>`
- **Respects existing network configurations** by reading current VM settings
- **PCI address management** (two strategies or auto-assignment)

## Configuration (mapping.yaml)

### Defaults Section

- `model`: NIC model (default: `virtio-net-pci`)
- `host_mtu`: MTU to set on tap back-end (0 omits host_mtu, default: 65535)
- `rx_queue_size`: Receive queue size (default: 1024)
- `tx_queue_size`: Transmit queue size (default: 256)
- `mac_prefix`: 3-byte MAC prefix (default: `bc:24:99`) – first byte is modified: OR 0x02 (locally administered) and clear multicast bit -> `bc` becomes `be`
- `udp_port_base`: Only the FIRST digit is used as the port prefix (e.g. 40000 -> prefix `4`)
- `udp_ip_by_vm`: Mapping VMID -> host IP for multi-node
- `udp_default_ip`: Fallback IP if VMID not in map (default: 127.0.0.1)
- `loopback_if_same_host`: If both endpoints resolve to same non-loopback IP, replace with 127.0.0.1 (default: true)
- `auto_pci_addr`: If true, omit bus/addr and let QEMU auto-assign
- `pci_alloc_strategy`: `lowest_free` (scan from 0x02) or `next_highest` (start just above highest existing virtio-net-pci; falls back to lowest_free if range exhausted)
- `links`: Array of `[vmA, ethA, vmB, ethB]`

### Link Example

```yaml
links:
  - [100, 1, 102, 1]
  - [101, 1, 102, 2]
```

## Usage

```bash
python3 gen_pve_links_args.py mapping.yaml > adjust_vm_config.sh
bash adjust_vm_config.sh
```

## Output Structure

For each VM a single `qm set <vmid> --args "<pairs>"` line is produced containing alternating:

- `-netdev socket,id=netN,udp=<remoteIP>:<remotePort>,localaddr=<localIP>:<localPort>`
- `-device virtio-net-pci,mac=...,netdev=netN[,bus=pci.0,addr=0xYY],id=netN,rx_queue_size=...,tx_queue_size=...[,host_mtu=...]`

### Full output example

```
qm set 100 --args "-netdev socket,id=net2,udp=127.0.0.1:41021,localaddr=127.0.0.1:41002 -device virtio-net-pci,mac=be:24:99:00:64:02,netdev=net2,bus=pci.0,addr=0x14,id=net2,rx_queue_size=1024,tx_queue_size=256,host_mtu=65535 -netdev socket,id=net3,udp=127.0.0.1:41031,localaddr=127.0.0.1:41003 -device virtio-net-pci,mac=be:24:99:00:64:03,netdev=net3,bus=pci.0,addr=0x15,id=net3,rx_queue_size=1024,tx_queue_size=256,host_mtu=65535"
```

## Port Allocation

`port = <first_digit_of_udp_port_base><VMID><ethIndex>`

Constraints enforced: VMID ≤ 999, ethIndex ≤ 9 → max 5-digit port plus leading prefix digit. Port must stay ≤ 65535 (validated).

## MAC Address Generation

Pattern: `<prefixAdjusted>:<vmid_hi_byte>:<vmid_lo_byte>:<iface_index>`

Example (prefix bc:24:99 → first byte becomes be):

- VM 100 (0x0064), eth1 → `be:24:99:00:64:01`

## NIC Reuse / Idempotency

- On each run the script derives deterministic MACs.
- If a MAC already exists in `qm showcmd` output, its `netN` index and PCI slot are reused; only the `-netdev` and `-device` arguments are regenerated (safe to reapply).
- New NICs start at `highest_existing_net_index + 1`.

## PCI Allocation

- Manual mode scans usable slots 0x02–0x1d (0x1e/0x1f reserved / bridges added to exclusion).
- `next_highest` tries strictly above current highest virtio-net-pci; if none free before 0x1e, falls back to `lowest_free`.
- Exhaustion error suggests enabling `auto_pci_addr:true` or removing devices.

## Multi-Host Usage

Set `udp_ip_by_vm` so each VM's traffic targets the correct physical host. Example:

```yaml
defaults:
  udp_ip_by_vm:
    100: 10.0.10.11   # node hosting VM 100
    102: 10.0.10.12   # node hosting VM 102
  udp_default_ip: 127.0.0.1
```

All `udp=` and `localaddr=` fields use these IPs. If both endpoints resolve to the *same* non-loopback IP and `loopback_if_same_host: true` (default), the script substitutes `127.0.0.1` on both sides to keep packets local.

MTU Note: When using non-loopback addresses, ensure the physical network MTU >= the `host_mtu` setting to avoid fragmentation.

## Limitations

- Only first digit of `udp_port_base` used.
- VMID limited to 3 digits; eth index to single digit for current port scheme.
- Does not remove obsolete NICs; it only appends/reuses.
- No dynamic MTU negotiation; ensure physical path supports configured MTU.

## Troubleshooting

- Port collision: adjust VMID/eth index or change `udp_port_base`.
- No free PCI slot (manual): switch to `auto_pci_addr:true` or free a slot.
- Want a clean slate: `qm set <vmid> --delete args`
- Unexpected reuse: change `mac_prefix` (will generate different MACs → new NICs).

## Safety Notes

- Review generated `qm set` lines before execution.
- Repeated application is safe; reused NICs keep PCI and net index.

## Requirements

- Python 3.6+
- PyYAML (`pip install pyyaml`)
- Proxmox VE host with `qm` available

## Disclaimer

Tool outputs raw QEMU args; validate in non-production first.
Then regenerate and re-apply.

## Disclaimer
Tool appends additional -netdev/-device pairs; review before applying in production.
