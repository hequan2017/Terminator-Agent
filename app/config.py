"""运行配置，全部从环境变量 / .env 读取。"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    # 用于会话签名和私钥加密，务必修改并妥善保存；改了之后已保存的私钥将无法解密
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    admin_username: str = field(default_factory=lambda: os.getenv("ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: os.getenv("ADMIN_PASSWORD", ""))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", str(BASE_DIR / "data" / "terminator.db")))

    # OpenAI 兼容接口（Claude / GLM / DeepSeek / Qwen / OpenAI 等均可）
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o"))
    llm_timeout: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT", "120")))

    # agent 单轮最多调用工具次数，防止死循环
    agent_max_steps: int = field(default_factory=lambda: int(os.getenv("AGENT_MAX_STEPS", "25")))
    # 单条命令默认超时（秒）
    command_timeout: int = field(default_factory=lambda: int(os.getenv("COMMAND_TIMEOUT", "60")))
    # 发版脚本单台机器超时（秒）
    deploy_timeout: int = field(default_factory=lambda: int(os.getenv("DEPLOY_TIMEOUT", "900")))
    # 是否校验 known_hosts；内网环境常为 false
    ssh_strict_host_key: bool = field(
        default_factory=lambda: os.getenv("SSH_STRICT_HOST_KEY", "false").lower() == "true"
    )


settings = Settings()
