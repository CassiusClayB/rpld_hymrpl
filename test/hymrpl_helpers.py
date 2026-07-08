"""
HyMRPL Test Helpers — Compatibilidade com rpld + motor adaptativo + FIFO seguro.

Funções utilitárias para garantir que os testes funcionem com o rpld atual:
  - set_battery_for_class(): seta /tmp/hymrpl_battery_<iface> para que o motor
    adaptativo concorde com a classe configurada (per-interface)
  - send_authenticated_cmd(): envia comando via hymrpl_cmd (autenticado)
  - ensure_token(): garanta que o token de autenticação existe
"""

import os
import re

TOKEN_PATH = "/etc/hymrpl/fifo.token"


def get_iface_name(node):
    """Get the 6LoWPAN interface name for a sensor node."""
    output = node.cmd('ip link show {}-pan0 2>/dev/null'.format(node.name))
    if 'does not exist' not in output and output.strip() and 'pan0' in output:
        return '{}-pan0'.format(node.name)
    output = node.cmd('ip link show 2>/dev/null')
    for line in output.split('\n'):
        m = re.match(r'\d+:\s+(lowpan\d+|wpan\d+):', line)
        if m:
            return m.group(1)
    return '{}-pan0'.format(node.name)


def get_battery_path(sensor):
    """Get the per-interface battery file path for a sensor."""
    iface = get_iface_name(sensor)
    return '/tmp/hymrpl_battery_{}'.format(iface)


def set_battery_for_class(sensor, node_class):
    """
    Seta a bateria simulada PER-INTERFACE para que o motor adaptativo
    do rpld concorde com a classe desejada.

    Classe S: bateria=100 (score alto → adaptativo mantém S)
    Classe N: bateria=10  (score baixo → adaptativo mantém N)
    """
    level = 100 if node_class == 'S' else 10
    path = get_battery_path(sensor)
    sensor.cmd('echo {} > {}'.format(level, path))


def set_battery_level(sensor, level):
    """Set specific battery level (0-100) for a sensor."""
    path = get_battery_path(sensor)
    sensor.cmd('echo {} > {}'.format(level, path))


def send_authenticated_cmd(sensor, cmd_str):
    """
    Envia comando autenticado via hymrpl_cmd.
    Retorna o output do comando.
    """
    output = sensor.cmd('hymrpl_cmd {} 2>&1'.format(cmd_str))
    return output.strip()


def ensure_token():
    """Garante que o token de autenticação FIFO existe."""
    if not os.path.exists(TOKEN_PATH):
        os.system('mkdir -p /etc/hymrpl')
        os.system('hymrpl_cmd --gen-token')


def setup_battery_for_topology(sensors, hybrid_classes):
    """
    Configura a bateria de cada sensor para que o motor adaptativo
    concorde com a classe definida em HYBRID_CLASSES.

    Deve ser chamado ANTES de start_rpld().
    """
    for s in sensors:
        cls = hybrid_classes.get(s.name, 'S')
        # Root não tem motor adaptativo, mas setamos mesmo assim
        set_battery_for_class(s, cls)


def force_class_n_nodes(sensors, hybrid_classes):
    """
    Após convergência, força nós que devem ser Classe N via FIFO autenticado.
    Deve ser chamado DEPOIS de start_rpld() e convergência.
    Espera rate limit entre cada comando.
    """
    import time
    for s in sensors:
        cls = hybrid_classes.get(s.name, 'S')
        if cls == 'N' and not s.params.get('dodag_root', False):
            s.cmd('hymrpl_cmd CLASS_N 2>/dev/null')
            time.sleep(2)
