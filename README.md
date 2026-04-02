# nncp-gen

A CLI utility to generate Kubernetes `NodeNetworkConfigurationPolicy` (NNCP) manifests from a declarative YAML config.

## Features

- **OVS bridges** with OVS internal interfaces and OVN bridge-mappings
- **Linux bridges** with static IP
- **Bonds** (LACP, active-backup, etc.) as uplinks
- **VLANs** as OVS access tags or standalone interfaces
- **Multi-node** — define once, generate per-node with unique IPs
- **Auto-naming** — policy names, interface names generated from conventions
- **Validation** — catches duplicate IPs, missing nodes, bad config
- **Dry-run** — preview without writing files
- **Flexible output** — per-file, single multi-doc YAML, or stdout

## Requirements

- Python 3.10+
- `pyyaml`
- `jinja2`

```bash
# Ubuntu/Debian
sudo apt install python3-yaml python3-jinja2

# Or via pip
pip install pyyaml jinja2
```

## Usage

```bash
# Preview what would be generated
python3 nncp_gen.py --config examples/cluster-network.yaml --dry-run

# Generate individual files
python3 nncp_gen.py --config examples/cluster-network.yaml --output ./manifests/

# Generate a single multi-doc YAML
python3 nncp_gen.py --config examples/cluster-network.yaml --output ./manifests/ --single-file

# Generate for one node only
python3 nncp_gen.py --config examples/cluster-network.yaml --node wrkr01

# Generate for one network only
python3 nncp_gen.py --config examples/cluster-network.yaml --network storage

# Validate config without generating
python3 nncp_gen.py --config examples/cluster-network.yaml --validate-only
```

## Config Format

```yaml
defaults:
  mtu: 9000
  bridge_type: ovs-bridge    # ovs-bridge | linux-bridge
  stp: false
  bond_mode: 802.3ad
  lacp_rate: fast

nodes:
  wrkr01:
    hostname: wrkr01.example.com
  wrkr02:
    hostname: wrkr02.example.com

networks:
  # OVS bridge with single NIC
  vm-access:
    bridge: br-vma
    nic: ens2f1
    vlan: 54
    allow_extra_patch_ports: true
    ovn_mapping: vm-access-br       # optional
    addresses:
      wrkr01: 172.25.54.23/24
      wrkr02: 172.25.54.24/24

  # OVS bridge with bond uplink
  storage:
    bridge: br-storage
    bond:
      name: bond0
      members: [ens1f0, ens1f1]
      mode: 802.3ad
    vlan: 100
    addresses:
      wrkr01: 172.25.100.23/24
      wrkr02: 172.25.100.24/24

  # Linux bridge
  mgmt:
    bridge: br-mgmt
    bridge_type: linux-bridge
    nic: ens1f0
    addresses:
      wrkr01: 10.0.1.11/24
      wrkr02: 10.0.1.12/24
```

## Config Reference

### `defaults`
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mtu` | int | none | Default MTU for all interfaces |
| `bridge_type` | string | `ovs-bridge` | `ovs-bridge` or `linux-bridge` |
| `stp` | bool | `false` | Spanning tree protocol |
| `bond_mode` | string | `802.3ad` | Bond mode |
| `lacp_rate` | string | `fast` | LACP rate |

### `nodes.<name>`
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `hostname` | string | no | FQDN for nodeSelector (defaults to node key) |

### `networks.<name>`
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `nic` | string | nic or bond | Physical interface name |
| `bond` | object | nic or bond | Bond config (see below) |
| `bridge` | string | no | Bridge name (auto-generated if omitted) |
| `bridge_type` | string | no | Override default bridge type |
| `vlan` | int | no | VLAN ID (access tag on OVS port) |
| `allow_extra_patch_ports` | bool | no | OVS bridge option |
| `ovn_mapping` | string/object | no | OVN bridge-mapping localnet name |
| `addresses` | map | no | node → CIDR address mapping |

### `networks.<name>.bond`
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | `bond0` | Bond interface name |
| `members` | list | required | Member NIC names |
| `mode` | string | `802.3ad` | Bond mode |
| `lacp_rate` | string | `fast` | LACP rate |
| `options` | map | none | Extra bond options |
