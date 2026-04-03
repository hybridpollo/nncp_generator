# nncp-gen

A CLI utility to generate Kubernetes `NodeNetworkConfigurationPolicy` (NNCP) manifests from a declarative YAML config.

## Features

- **Role-based targeting** — one NNCP per Kubernetes node role (worker, master, infra, etc.)
- **Node-based targeting** — one NNCP per individual node with per-node static IPs
- **OVS bridges** with OVS internal interfaces and OVN bridge-mappings
- **Linux bridges** with static IP
- **Bonds** (LACP, active-backup, etc.) as uplinks
- **VLANs** as OVS access tags or standalone interfaces
- **Multi-node** — define once, generate per-node with unique IPs
- **Auto-naming** — policy names, interface names generated from conventions
- **Validation** — catches duplicate IPs, missing nodes/roles, bad config
- **Dry-run** — preview without writing files
- **Extra ports** — add additional ports (NICs, OVS interfaces) to any bridge
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

# Role-based: targets nodes by label selector
roles:
  worker:
    node_selector:
      node-role.kubernetes.io/worker: ""
  infra:
    node_selector:
      node-role.kubernetes.io/infra: ""
      cluster.example.com/zone: dmz

# Node-based: targets individual nodes by hostname
nodes:
  wrkr01:
    hostname: wrkr01.example.com
  wrkr02:
    hostname: wrkr02.example.com

networks:
  # Role-based: one NNCP for all workers (no per-node IPs)
  vm-access:
    role: worker                        # targets the 'worker' role
    bridge: br-vma
    nic: ens2f1
    vlan: 54
    allow_extra_patch_ports: true
    ovn_mapping: vm-access-br

  # Node-based: one NNCP per node with static IPs
  storage:
    bridge: br-storage
    nic: ens1f0
    vlan: 100
    addresses:                          # per-node IPs (node-based only)
      wrkr01: 172.25.100.23/24
      wrkr02: 172.25.100.24/24

  # Extra ports: add OVS interfaces or NICs to a bridge
  vm-storage:
    role: worker
    bridge: br-vm-stor
    nic: ens2f1
    allow_extra_patch_ports: true
    ovn_mapping: vm-storage-br
    extra_ports:
      - name: ens2f2                    # plain NIC port
      - name: ovs-storage0             # OVS interface with static IP + VLAN
        type: ovs-interface
        mtu: 9000
        ipv4: 172.25.54.23/24
        vlan:
          mode: access
          tag: 54
      - name: ovs-internal0            # OVS interface without IP
        type: ovs-interface

  # Extra ports with per-node IPs (node-based)
  storage:
    bridge: br-storage
    nic: ens3f0
    extra_ports:
      - name: ovs-stor0
        type: ovs-interface
        vlan:
          mode: access
          tag: 55
        addresses:                      # unique IP per node
          wrkr01: 172.25.55.23/24
          wrkr02: 172.25.55.24/24
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

### `roles.<name>`
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `node_selector` | map | yes | Label key/value pairs for nodeSelector |

### `nodes.<name>`
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `hostname` | string | no | FQDN for nodeSelector (defaults to node key) |

### `networks.<name>`
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `role` | string | no | Target a role instead of individual nodes |
| `nic` | string | nic or bond | Physical interface name |
| `bond` | object | nic or bond | Bond config (see below) |
| `bridge` | string | no | Bridge name (auto-generated if omitted) |
| `bridge_type` | string | no | Override default bridge type |
| `vlan` | int | no | VLAN ID (access tag on OVS port) |
| `allow_extra_patch_ports` | bool | no | OVS bridge option |
| `ovn_mapping` | string/object | no | OVN bridge-mapping localnet name |
| `addresses` | map | no | node → CIDR mapping (node-based only, not with role) |
| `extra_ports` | list | no | Additional ports to add to the bridge (see below) |

### `networks.<name>.extra_ports[]`
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Port/interface name |
| `type` | string | no | Set to `ovs-interface` to generate an OVS internal interface |
| `mtu` | int | no | MTU override (defaults to network/global MTU) |
| `ipv4` | string | no | Static IP in CIDR format, e.g. `172.25.54.23/24` (ovs-interface only, same IP for all nodes) |
| `addresses` | map | no | Per-node IPs: `node_name: CIDR` (ovs-interface only, takes priority over `ipv4`) |
| `vlan` | object | no | VLAN config for this port (`mode` + `tag`) |

### `networks.<name>.bond`
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | `bond0` | Bond interface name |
| `members` | list | required | Member NIC names |
| `mode` | string | `802.3ad` | Bond mode |
| `lacp_rate` | string | `fast` | LACP rate |
| `options` | map | none | Extra bond options |

---

## ⚠️ Disclaimer

This code was generated by an AI (hi, I'm Bibbles 💙). While it has been tested and reviewed by a human, it comes with absolutely no warranty, express or implied.

**You break it, you buy it.**

By using this tool, you acknowledge that:

- You are responsible for reviewing all generated NNCP manifests **before** applying them to a cluster.
- Misconfigured network policies can and will take your nodes offline, make you question your career choices, and potentially ruin your weekend.
- The author(s) — human and AI alike — are not responsible for any outages, data loss, broken bonds (the network kind or otherwise), or existential dread caused by applying untested manifests to production.
- `--dry-run` exists for a reason. Use it.

**Test in dev. Review the output. Don't blindly `oc apply` in prod.**

You have been warned. 🫡
