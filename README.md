# Proxmox QEMU UDP Link Generator

Simplified Python helper to add point‑to‑point ("cable") links between Proxmox VMs using QEMU user‑space UDP sockets. It inspects existing VM config files to continue `netN` numbering, generates deterministic MACs, and emits `qm set` command lines you can paste / execute.

## Features
- UDP only (no vhost-user, no memory backends)
- Deterministic MAC addresses from a 3‑byte prefix + VMID + interface index
- Deterministic UDP port formula: `port = udp_port_base + <VMID> + <netIndex>`
- Automatically discovers highest existing `netN` in `/etc/pve/qemu-server/<vmid>.conf` and appends new NICs sequentially
- Single YAML mapping drives all links

## Port Formula
Requested scheme: concatenate prefixDigit (first digit of `udp_port_base`), the VMID, and the eth index you specify in the link list.

```
port = int(f"{prefixDigit}{VMID}{ethIndex}")
```
Example: `udp_port_base: 40000` -> `prefixDigit = '4'`.
VM 101, eth 2 -> `"4" + "101" + "2" = "41012"` => port 41012.

Notes:
- `ethIndex` is the number you put in the link (second or fourth element), not the resulting `netN` index.
- Collisions are detected; script exits if two endpoints would share the same port.
- Ports must be <= 65535. Large VMIDs or indices can overflow; choose a different `udp_port_base` with a smaller leading digit if needed (e.g. 30000 -> prefixDigit '3').

Each link uses two independently bound sockets (one per endpoint) with cross target configuration:
- VM A: `localaddr=<A_IP>:portA` and `udp=<B_IP>:portB`
- VM B: `localaddr=<B_IP>:portB` and `udp=<A_IP>:portA`

## MAC Generation
MAC = `{prefix0}:{prefix1}:{prefix2}:{vmid_hi}:{vmid_lo}:{iface}`
- Prefix: forced to locally administered & unicast (multicast bit cleared, local bit set)
- `vmid_hi` / `vmid_lo`: high / low byte of VMID
- `iface`: eth index you put in the link (not the netIndex)

Example (prefix `3c:ec:ef`, VM 100, eth 1) -> `3e:ec:ef:00:64:01` (first octet adjusted to `3e`).

## Requirements
- Proxmox host (script runs on a node with access to `/etc/pve/qemu-server/*`)
- Python 3.8+ (PyYAML)

Install PyYAML if needed:
```bash
pip install pyyaml
```
(Or apt: `apt install python3-yaml`)

## Files
- `gen_pve_links_args.py` – generator script
- `mapping.yaml` – configuration

## `mapping.yaml` Structure (UDP Only)
```yaml
defaults:
  model: virtio-net-pci
  host_mtu: 65535
  rx_queue_size: 256
  tx_queue_size: 256
  mac_prefix: "3c:ec:ef"
  udp_port_base: 40000
  udp_ip_by_vm: {}          # optional: VMID -> IP for multi-host (see Multi-Host)
  udp_default_ip: 127.0.0.1 # fallback

links:
  - [100, 1, 102, 1]
  - [101, 1, 102, 2]
  # [vmA, ethA, vmB, ethB]
```
Notes:
- `eth` numbers are *logical* identifiers used for MAC derivation only; they do **not** need to match `netIndex`.

## Running
Generate plan:
```bash
python3 gen_pve_links_args.py mapping.yaml > plan.txt
```
Inspect `plan.txt`; then apply:
```bash
bash plan.txt
```
Start (or restart) the VMs afterwards:
```bash
qm start 100
qm start 101
# etc.
```

## Sample Output Snippet
```
update VM 100: -args -netdev socket,id=c_100_1_102_1_A,udp=127.0.0.1:40103,localaddr=127.0.0.1:40101 -device virtio-net-pci,mac=3e:ec:ef:00:64:01,rx_queue_size=256,tx_queue_size=256,netdev=c_100_1_102_1_A,id=net1,host_mtu=65535 ...
```
Meaning: VM 100 gains `net1` (existing net0 kept), binding to port 40101 and sending to the peer's port 40103.

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

MTU Note: When using non-loopback underlay, ensure the physical/underlay path MTU >= the `host_mtu` you request. If unsure, pick a conservative value (e.g. 9000 for jumbo networks, 1500 standard) to avoid fragmentation or silent drops.

## Collision & Range Considerations
- Port = concat(prefixDigit, VMID, ethIndex). Example collisions: VM 10 eth 11 -> 41011 vs VM 101 eth 1 -> 41011. The script aborts if detected.
- Keep result <= 65535; with prefix '4' the largest safe pattern is limited. Large VMIDs (>= 1000) quickly exceed range.
- To widen space, choose a smaller single-digit prefix (e.g. base 20000 -> prefix '2'). Multi-digit prefixes are currently ignored except first digit.
- Changing eth indices in links changes ports; keep stable indexing for reproducibility.

## Updating Links
1. Edit `mapping.yaml`
2. Re-run the generator to a new plan file
3. Apply plan (only changed args are updated by `qm set`)
4. Restart affected VMs if needed

## Removing Generated NICs
Manually edit `/etc/pve/qemu-server/<vmid>.conf` to remove unwanted `args:` portion or rebuild arguments from scratch. (The script only *adds*.)

## Limitations / Future Ideas
- Only first digit of `udp_port_base` used (could be extended to configurable string prefix).
- No automatic pruning of removed links / NICs.
- No helper to regenerate a full --args clean slate (manual for now).
- Could add an optional alternate formula (hash-based) to compress larger VMIDs.
- No automatic cleanup of removed links.
- Single direction loss is silent; consider using monitoring (e.g., `tcpdump -ni any udp port <port>`).

## Security
Traffic is unencrypted & unauthenticated. For sensitive environments, tunnel UDP over WireGuard / IPsec between hosts, or use an isolated backend network/VLAN.

## License
Choose and add a license (e.g., MIT) before publishing.

## Quick TL;DR
```bash
pip install pyyaml
python3 gen_pve_links_args.py mapping.yaml > plan.txt
bash plan.txt
qm start <vmid>
```

Contributions welcome via PRs (validation, collision detection, link removal helper, docs improvements).
