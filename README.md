# PVE UDP Network Link Generator

Generates Proxmox VE (PVE) QEMU arguments for creating direct UDP-based network links between VMs.

## Overview

This tool automates the creation of point-to-point network connections between Proxmox VMs using UDP sockets. It generates the necessary QEMU command-line arguments to establish these links without requiring traditional bridges or VLANs.

## Features

- **Direct VM-to-VM networking** using UDP sockets
- **Deterministic MAC address generation** based on VMID and interface index
- **Multi-host support** with configurable IP mappings
- **Automatic port allocation** using formula: `<prefix><VMID><ethIndex>`
- **Respects existing network configurations** by reading current VM settings
- **PCI address management** to avoid conflicts

## Configuration (mapping.yaml)

### Defaults Section

- `model`: NIC model (default: `virtio-net-pci`)
- `host_mtu`: Maximum transmission unit (0 to omit, default: 65535)
- `rx_queue_size`: Receive queue size (default: 1024)
- `tx_queue_size`: Transmit queue size (default: 256)
- `mac_prefix`: 3-byte MAC address prefix (default: `bc:24:99`)
- `udp_port_base`: Base port for UDP connections (default: 40000)
- `udp_ip_by_vm`: Optional VMID to host IP mapping for multi-host setups
- `udp_default_ip`: Fallback IP when VMID not in mapping (default: 127.0.0.1)
- `loopback_if_same_host`: Use loopback for same-host connections (default: true)

### Links Section

Define connections as: `[vmA, ethA, vmB, ethB]`

Example:
```yaml
links:
  - [100, 1, 102, 1]  # Connect VM100 eth1 to VM102 eth1
  - [101, 1, 102, 2]  # Connect VM101 eth1 to VM102 eth2
```

## Usage

```bash
./gen_pve_links_args.py mapping.yaml
```

This generates `qm set` commands that can be executed to apply the network configuration:

```bash
# VM 100
qm set 100 --args "-netdev socket,id=net0,udp=127.0.0.1:41021,localaddr=127.0.0.1:41001 -device virtio-net-pci,mac=be:24:99:00:64:01,..."
```

## Port Allocation

Ports are allocated using the formula: `<first_digit_of_base><VMID><eth_index>`

Example with `udp_port_base: 40000`:
- VM 100, eth1 → port 41001
- VM 102, eth1 → port 41021

## MAC Address Generation

MAC addresses follow the pattern: `<prefix>:<vmid_hi>:<vmid_lo>:<iface_idx>`

Example with prefix `bc:24:99`:
- VM 100, eth1 → `be:24:99:00:64:01`

The first byte has the locally administered bit (0x02) set to avoid OUI conflicts.

## Requirements

- Python 3.6+
- PyYAML (`pip install pyyaml`)
- Proxmox VE environment with `qm` command available

## Limitations

- VMIDs must be ≤ 999 (3 digits)
- Ethernet indices must be ≤ 9 (single digit)
- Generated ports must be ≤ 65535

## Sample Output Snippet
```
qm set 100 --args "-netdev socket,id=c_100_1_102_1_A,udp=127.0.0.1:40103,localaddr=127.0.0.1:40101 -device virtio-net-pci,mac=be:24:99:00:64:01,rx_queue_size=1024,tx_queue_size=256,netdev=c_100_1_102_1_A,id=net1,host_mtu=65535"
```

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
- Only first digit of `udp_port_base` used for port calculation
- No automatic cleanup of removed links
- VMIDs limited to 999, eth indices to 9 for current port scheme

## Usage
```bash
pip install pyyaml
python3 gen_pve_links_args.py mapping.yaml > adjust_vm_config.sh
bash adjust_vm_config.sh
qm start <vmid>
```
