"""Minimal interactive loop for a CLI-first code agent."""


class ZeroCodeAgent:
    """Small placeholder agent we will extend step-by-step."""

    def handle_user_message(self, user_text: str) -> str:
        normalized = user_text.strip()
        if not normalized:
            return "请输入内容，我再继续。"
        return f"[zero-code v0.1] 收到你的请求：{normalized}"


def run_cli() -> None:
    agent = ZeroCodeAgent()
    print("Zero Code CLI 已启动。输入 exit 或 quit 退出。")

    while True:
        try:
            user_text = input("you> ")
        except EOFError:
            print("\n检测到 EOF，退出。")
            break
        except KeyboardInterrupt:
            print("\n检测到中断，退出。")
            break

        if user_text.strip().lower() in {"exit", "quit"}:
            print("再见。")
            break

        reply = agent.handle_user_message(user_text)
        print(f"agent> {reply}")
