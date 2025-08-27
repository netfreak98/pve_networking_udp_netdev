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
- **Deterministic MAC address generation** based on VMID and interface index
- **Multi-host support** with configurable IP mappings
- **Automatic port allocation** using formula: `<prefix><VMID><ethIndex>`
- **Respects existing network configurations** by reading current VM settings
- **PCI address management** to avoid conflicts and to make sure generated socket-nics are added `after` normal bridge interfaces

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

## Usage
```bash
pip install pyyaml
python3 gen_pve_links_args.py mapping.yaml > adjust_vm_config.sh
bash adjust_vm_config.sh
qm start <vmid>
```

## Outputs from a example running VM:

LLDP:
```bash
veos-pe01>show lldp neighbors 
Last table change time   : 0:03:46 ago
Number of table inserts  : 2
Number of table deletes  : 0
Number of table drops    : 0
Number of table age-outs : 0

Port          Neighbor Device ID       Neighbor Port ID    TTL
---------- ------------------------ ---------------------- ---
Et1           veos-c01                  Ethernet1           120
Et2           veos-c02                  Ethernet1           120
```

ISIS:
```bash
veos-pe01>show isis neighbors 
 
Instance  VRF      System Id        Type Interface          SNPA              State Hold time   Circuit Id          
core      default  veos-c01          L2   Ethernet1.101      P2P               UP    24          1D                  
```
