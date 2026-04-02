#!/usr/bin/env python3
"""
nncp-gen: A CLI utility to generate NodeNetworkConfigurationPolicy (NNCP) manifests
for kubernetes-nmstate from a declarative YAML config.

Supports: OVS bridges, Linux bridges, bonds, VLANs, OVN bridge-mappings.
"""

import argparse
import ipaddress
import os
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_cidr(cidr_str):
    """Parse '172.25.54.23/24' into (address, prefix_length)."""
    net = ipaddress.ip_interface(cidr_str)
    return str(net.ip), int(net.network.prefixlen)


def auto_policy_name(network_name, node_name):
    """Generate a policy name like pol-storage-wrkr01."""
    return f"pol-{network_name}-{node_name}"


def auto_ovs_iface_name(network_name, index=0):
    """Generate an OVS interface name like ovs-storage0."""
    return f"ovs-{network_name}{index}"


def auto_bridge_name(network_name, bridge_type):
    """Generate a bridge name like br-storage."""
    return f"br-{network_name}"


def auto_bond_name(index=0):
    """Generate a bond name like bond0."""
    return f"bond{index}"


def auto_vlan_name(base_iface, vlan_id):
    """Generate a VLAN interface name like bond0.100."""
    return f"{base_iface}.{vlan_id}"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    pass


def validate_config(config):
    """Validate the config and return a list of errors (empty = valid)."""
    errors = []

    if 'nodes' not in config or not config['nodes']:
        errors.append("No 'nodes' defined in config.")

    if 'networks' not in config or not config['networks']:
        errors.append("No 'networks' defined in config.")

    if errors:
        return errors

    # Check for duplicate IPs across all networks
    all_ips = {}
    for net_name, net_cfg in config.get('networks', {}).items():
        addresses = net_cfg.get('addresses', {})
        for node_name, addr in addresses.items():
            if node_name not in config.get('nodes', {}):
                errors.append(
                    f"Network '{net_name}' references node '{node_name}' "
                    f"which is not defined in 'nodes'."
                )
            if addr:
                ip_str, _ = parse_cidr(addr)
                key = f"{ip_str}"
                if key in all_ips:
                    errors.append(
                        f"Duplicate IP {ip_str}: used in network '{net_name}' "
                        f"for node '{node_name}' and also in "
                        f"'{all_ips[key]}'."
                    )
                else:
                    all_ips[key] = f"{net_name}/{node_name}"

    # Check required fields per network
    for net_name, net_cfg in config.get('networks', {}).items():
        bridge_type = net_cfg.get('bridge_type',
                                   config.get('defaults', {}).get('bridge_type', 'ovs-bridge'))
        if bridge_type not in ('ovs-bridge', 'linux-bridge'):
            errors.append(
                f"Network '{net_name}': invalid bridge_type '{bridge_type}'. "
                f"Must be 'ovs-bridge' or 'linux-bridge'."
            )

        # Must have at least a NIC or bond members
        if not net_cfg.get('nic') and not net_cfg.get('bond'):
            errors.append(
                f"Network '{net_name}': must specify 'nic' or 'bond'."
            )

    return errors


# ---------------------------------------------------------------------------
# NNCP Builder
# ---------------------------------------------------------------------------

def build_nncp_context(node_name, node_cfg, net_name, net_cfg, defaults):
    """Build the Jinja2 template context for one NNCP (one node + one network)."""

    hostname = node_cfg.get('hostname', node_name)
    mtu = net_cfg.get('mtu', defaults.get('mtu'))
    bridge_type = net_cfg.get('bridge_type', defaults.get('bridge_type', 'ovs-bridge'))
    stp = net_cfg.get('stp', defaults.get('stp', False))
    allow_extra_patch_ports = net_cfg.get('allow_extra_patch_ports',
                                           defaults.get('allow_extra_patch_ports', False))

    # Address for this node
    address_str = net_cfg.get('addresses', {}).get(node_name)
    node_ip = None
    if address_str:
        ip, prefix = parse_cidr(address_str)
        node_ip = {'address': ip, 'prefix_length': prefix}

    # Policy name
    policy_name = net_cfg.get('policy_name_template')
    if policy_name:
        policy_name = policy_name.format(network=net_name, node=node_name)
    else:
        policy_name = auto_policy_name(net_name, node_name)

    # --- Build interface lists ---
    nics = []
    bonds = []
    ovs_interfaces = []
    ovs_bridges = []
    linux_bridges = []
    vlans = []
    ovn_mappings = []

    # What's the uplink? (bond or nic)
    uplink_name = None
    bond_cfg = net_cfg.get('bond')
    nic_name = net_cfg.get('nic')

    if bond_cfg:
        # Bond with member NICs
        members = bond_cfg.get('members', [])
        bond_name = bond_cfg.get('name', auto_bond_name(0))
        for member in members:
            nics.append({'name': member, 'mtu': mtu})
        bonds.append({
            'name': bond_name,
            'mtu': mtu,
            'members': members,
            'mode': bond_cfg.get('mode', defaults.get('bond_mode', '802.3ad')),
            'lacp_rate': bond_cfg.get('lacp_rate', defaults.get('lacp_rate', 'fast')),
            'options': bond_cfg.get('options', {}),
        })
        uplink_name = bond_name
    elif nic_name:
        nics.append({'name': nic_name, 'mtu': mtu})
        uplink_name = nic_name

    # VLAN config (standalone, not OVS port VLAN)
    vlan_cfg = net_cfg.get('vlan')
    vlan_on_bridge = net_cfg.get('vlan_on_bridge')  # VLAN as OVS port tag

    # Bridge
    bridge_name = net_cfg.get('bridge', auto_bridge_name(net_name, bridge_type))

    if bridge_type == 'ovs-bridge':
        # OVS interface is only created when the node has an IP address
        # assigned for this network (e.g., host management on the bridge).
        # For pure VM traffic bridges, no OVS interface is needed.
        ovs_iface_name = None
        if node_ip:
            ovs_iface_name = net_cfg.get('ovs_interface',
                                          auto_ovs_iface_name(net_name))
            ovs_iface = {
                'name': ovs_iface_name,
                'mtu': mtu,
                'ipv4': node_ip,
            }
            ovs_interfaces.append(ovs_iface)

        # Build port list
        ports = []
        # Uplink port
        uplink_port = {'name': uplink_name}
        ports.append(uplink_port)

        # OVS interface port (only if we created one)
        if ovs_iface_name:
            ovs_port = {'name': ovs_iface_name}
            if vlan_on_bridge:
                ovs_port['vlan'] = {
                    'mode': vlan_on_bridge.get('mode', 'access'),
                    'tag': vlan_on_bridge.get('tag', vlan_on_bridge.get('id')),
                }
            elif vlan_cfg:
                # If there's a vlan config, apply as access tag on the OVS port
                ovs_port['vlan'] = {
                    'mode': 'access',
                    'tag': vlan_cfg if isinstance(vlan_cfg, int) else vlan_cfg.get('id'),
                }
            ports.append(ovs_port)

        ovs_bridges.append({
            'name': bridge_name,
            'mtu': mtu,
            'stp': stp,
            'allow_extra_patch_ports': allow_extra_patch_ports,
            'ports': ports,
        })

        # OVN bridge mapping
        ovn_mapping = net_cfg.get('ovn_mapping')
        if ovn_mapping:
            if isinstance(ovn_mapping, str):
                ovn_mappings.append({
                    'localnet': ovn_mapping,
                    'bridge': bridge_name,
                    'state': 'present',
                })
            elif isinstance(ovn_mapping, dict):
                ovn_mappings.append({
                    'localnet': ovn_mapping.get('localnet', ovn_mapping.get('name')),
                    'bridge': ovn_mapping.get('bridge', bridge_name),
                    'state': ovn_mapping.get('state', 'present'),
                })

    elif bridge_type == 'linux-bridge':
        # Linux bridge gets the IP directly
        bridge = {
            'name': bridge_name,
            'mtu': mtu,
            'stp': stp,
            'ports': [{'name': uplink_name}],
        }
        if node_ip:
            bridge['ipv4'] = node_ip
        linux_bridges.append(bridge)

    # Standalone VLAN interface (on top of bridge or uplink)
    if vlan_cfg and isinstance(vlan_cfg, dict) and vlan_cfg.get('standalone'):
        vlan_base = vlan_cfg.get('base_iface', bridge_name)
        vlan_id = vlan_cfg['id']
        vlan_entry = {
            'name': vlan_cfg.get('name', auto_vlan_name(vlan_base, vlan_id)),
            'mtu': mtu,
            'base_iface': vlan_base,
            'id': vlan_id,
        }
        if node_ip and not ovs_interfaces:
            vlan_entry['ipv4'] = node_ip
        vlans.append(vlan_entry)

    return {
        'policy_name': policy_name,
        'hostname': hostname,
        'nics': nics,
        'bonds': bonds,
        'ovs_interfaces': ovs_interfaces,
        'ovs_bridges': ovs_bridges,
        'linux_bridges': linux_bridges,
        'vlans': vlans,
        'ovn_mappings': ovn_mappings,
    }


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def render_nncp(context, template_dir=None):
    """Render an NNCP YAML from context using Jinja2."""
    if template_dir is None:
        template_dir = str(Path(__file__).parent / 'templates')

    env = Environment(
        loader=FileSystemLoader(template_dir),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template('nncp.yaml.j2')
    return template.render(**context)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Generate NNCP manifests from a declarative YAML config.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --config cluster-network.yaml
  %(prog)s --config cluster-network.yaml --output ./manifests/
  %(prog)s --config cluster-network.yaml --dry-run
  %(prog)s --config cluster-network.yaml --node wrkr01
  %(prog)s --config cluster-network.yaml --network storage
        """
    )
    parser.add_argument('-c', '--config', required=True,
                        help='Path to the YAML config file.')
    parser.add_argument('-o', '--output', default=None,
                        help='Output directory for generated NNCP files. '
                             'If not set, prints to stdout.')
    parser.add_argument('-n', '--node', default=None,
                        help='Generate NNCPs only for this node.')
    parser.add_argument('--network', default=None,
                        help='Generate NNCPs only for this network.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview generated NNCPs without writing files.')
    parser.add_argument('--validate-only', action='store_true',
                        help='Only validate the config, do not generate.')
    parser.add_argument('--single-file', action='store_true',
                        help='Output all NNCPs in a single multi-document YAML.')
    parser.add_argument('--template-dir', default=None,
                        help='Custom Jinja2 template directory.')
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Validate
    errors = validate_config(config)
    if errors:
        print("Validation errors:", file=sys.stderr)
        for err in errors:
            print(f"  ✗ {err}", file=sys.stderr)
        sys.exit(1)

    if args.validate_only:
        print("✓ Config is valid.")
        sys.exit(0)

    defaults = config.get('defaults', {})
    nodes = config.get('nodes', {})
    networks = config.get('networks', {})

    # Filter if requested
    if args.node:
        if args.node not in nodes:
            print(f"Error: Node '{args.node}' not found in config.", file=sys.stderr)
            sys.exit(1)
        nodes = {args.node: nodes[args.node]}

    if args.network:
        if args.network not in networks:
            print(f"Error: Network '{args.network}' not found in config.", file=sys.stderr)
            sys.exit(1)
        networks = {args.network: networks[args.network]}

    # Generate
    all_docs = []
    file_outputs = []

    for net_name, net_cfg in networks.items():
        for node_name, node_cfg in nodes.items():
            # Skip nodes that don't have an address for this network
            # (unless no addresses are defined at all)
            if net_cfg.get('addresses') and node_name not in net_cfg['addresses']:
                continue

            if node_cfg is None:
                node_cfg = {}

            context = build_nncp_context(
                node_name, node_cfg, net_name, net_cfg, defaults
            )
            rendered = render_nncp(context, args.template_dir)
            all_docs.append(rendered)
            file_outputs.append({
                'filename': f"{context['policy_name']}.yaml",
                'content': rendered,
                'node': node_name,
                'network': net_name,
            })

    if not all_docs:
        print("No NNCPs generated. Check your config.", file=sys.stderr)
        sys.exit(1)

    # Output
    if args.dry_run:
        print(f"# Dry run: {len(all_docs)} NNCP(s) would be generated\n")
        for doc in all_docs:
            print(doc)
            print("---")
        sys.exit(0)

    if args.output:
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)

        if args.single_file:
            combined = "---\n".join(all_docs)
            out_path = out_dir / "nncps.yaml"
            with open(out_path, 'w') as f:
                f.write(combined)
            print(f"✓ Wrote {len(all_docs)} NNCP(s) to {out_path}")
        else:
            for item in file_outputs:
                out_path = out_dir / item['filename']
                with open(out_path, 'w') as f:
                    f.write(item['content'])
                print(f"✓ {out_path}")
            print(f"\n✓ Generated {len(file_outputs)} NNCP file(s) in {out_dir}/")
    else:
        # Print to stdout
        if args.single_file or len(all_docs) > 1:
            print("---\n".join(all_docs))
        else:
            print(all_docs[0])


if __name__ == '__main__':
    main()
