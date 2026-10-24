"""Interactive confirmation and explicit recovery for CAD-only runs."""
import sys


def interactive(result, config):
    from .workflow import resume_cad
    if not sys.stdin.isatty():
        return result
    while result.get('workflow_status') in {'paused', 'failed'}:
        state = result['cad_state']
        print('\nCAD step:', state['step'])
        print('Working CAD:', state.get('working_geometry', ''))
        print('Groups / roles:', state.get('roles', {}))
        print(state.get('question') if state['status'] == 'paused' else state.get('error'))
        try:
            if state.get('pause_kind') == 'clarification':
                answer = input('Supplement (or exit / cancel): ').strip()
                if answer in {'exit', 'cancel'}:
                    action, supplement = answer, ''
                elif answer:
                    action, supplement = 'clarify', answer
                else:
                    continue
                roles = None
            else:
                recovery = state['status'] == 'failed' or state.get('pause_kind') == 'recovery'
                choices = 'retry / clarify / reopen / exit / cancel' if recovery else 'confirm / save / exit / cancel'
                action = input('Action [' + choices + ']: ').strip().lower()
                if action == 'confirm':
                    action = 'approve'
                if action not in {'approve', 'save', 'exit', 'cancel', 'retry', 'reopen', 'clarify'}:
                    continue
                roles = {}
                if action == 'approve' and state.get('pause_kind') == 'boundary_roles':
                    for name, role in state['roles'].items():
                        if not role:
                            roles[name] = input('Role for ' + name + ': ').strip()
                    if any(not role for role in roles.values()):
                        continue
                supplement = input('Additional CAD instructions: ').strip() if action == 'clarify' else ''
        except (EOFError, KeyboardInterrupt):
            print('\nCAD remains saved at its current checkpoint.')
            break
        if action == 'exit':
            break
        result = resume_cad(config, state['run_dir'], action=action, supplement=supplement, roles=roles)
        if action == 'save':
            break
    return result
