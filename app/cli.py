import argparse
import getpass
import json
import sys
from .auth import AuthError, AuthService, constant_equal, create_owner_from_prompt
from . import migration
from .account_admin import create_user, set_status


def interactive_terminal():
    if sys.stdin is None or not sys.stdin.isatty():
        return False
    if sys.platform == "win32":
        # Windows CRT treats NUL as a TTY, but getpass reads a real console.
        import ctypes
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.GetConsoleMode.restype = ctypes.c_int
        mode = ctypes.c_uint32()
        try:
            return bool(kernel.GetConsoleMode(msvcrt.get_osfhandle(sys.stdin.fileno()), ctypes.byref(mode)))
        except (OSError, ValueError):
            return False
    return True


def main():
    p = argparse.ArgumentParser(description="Offline copy migration and account/session operations")
    p.add_argument(
        "action",
        choices=[
            "copy-v1",
            "initialize-empty",
            "create-owner",
            "create-user",
            "enable-user",
            "disable-user",
            "reset-password",
            "revoke-sessions",
        ],
    )
    p.add_argument("--database", required=True)
    p.add_argument("--username")
    p.add_argument("--display-name", default="Validation owner")
    p.add_argument("--user-id")
    p.add_argument("--source", help="Read-only source for copy-v1; destination must not exist")
    args = p.parse_args()
    try:
        if args.action in ("create-owner", "create-user", "reset-password") and not interactive_terminal():
            raise AuthError(422, "Interactive terminal required for hidden password input")
        if args.action == "copy-v1":
            if not args.source:
                raise AuthError(422, "Explicit source required")
            migration.backup(args.source, args.database)
            print(json.dumps({"completed": True, "action": "copy-v1", "source_modified": False}))
        elif args.action == "initialize-empty":
            migration.initialize_empty_v1(args.database)
            print(json.dumps({"completed": True, "action": "initialize-empty", "owner_required": True}))
        elif args.action == "create-owner":
            if not args.username:
                raise AuthError(422, "Account name required")
            result = create_owner_from_prompt(args.database, args.username, args.display_name)
            print(json.dumps({k: v for k, v in result.items() if k != "source_logical_fingerprint"}))
        elif args.action == "create-user":
            if not args.username:
                raise AuthError(422, "Account name required")
            password = getpass.getpass("New user password: ")
            if not constant_equal(password, getpass.getpass("Confirm password: ")):
                raise AuthError(422, "Password confirmation mismatch")
            print(
                json.dumps(
                    create_user(AuthService(args.database), args.username, args.display_name, password)
                )
            )
        elif args.action in ("enable-user", "disable-user"):
            if not args.user_id:
                raise AuthError(422, "User id required")
            set_status(
                AuthService(args.database),
                args.user_id,
                "active" if args.action == "enable-user" else "disabled",
            )
            print(json.dumps({"completed": True, "action": args.action, "user_id": args.user_id}))
        else:
            if not args.user_id:
                raise AuthError(422, "User id required")
            service = AuthService(args.database)
            password = None
            if args.action == "reset-password":
                password = getpass.getpass("New password: ")
                if not constant_equal(password, getpass.getpass("Confirm password: ")):
                    raise AuthError(422, "Password confirmation mismatch")
            service.offline_revoke(args.user_id, password)
            print(json.dumps({"completed": True, "action": args.action}))
        return 0
    except AuthError as exc:
        print(json.dumps({"detail": exc.detail}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"detail": "Operation failed; inspect isolated state"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
