"""Shared CAD-only arguments; importing these requires no model or Ansys SDK."""


def add_cad_arguments(parser):
    parser.add_argument('--geometry_path', help='One SpaceClaim .scdoc input (WSL or Windows path).')
    parser.add_argument('--stop-after', dest='stop_after', choices=['cad'], default=None)
    parser.add_argument('--resume', help='Resume an existing CAD task directory.')
    parser.add_argument('--ui-mode', choices=['gui', 'hidden'], default='gui')


def validate_cad_arguments(parser, args):
    if args.resume:
        if args.geometry_path or args.case_path or args.custom_mesh_path or args.prompt_path or args.stop_after:
            parser.error('--resume uses the saved task; do not combine it with new inputs.')
    elif args.geometry_path:
        if args.case_path or args.custom_mesh_path:
            parser.error('CAD input cannot be combined with case or external mesh input.')
        if args.stop_after != 'cad':
            parser.error('--geometry_path currently requires --stop-after cad.')
        if not args.prompt_path:
            parser.error('--geometry_path requires --prompt_path.')
    elif args.stop_after:
        parser.error('--stop-after cad requires --geometry_path.')
