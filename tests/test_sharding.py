from pathlib import Path

from docforge_enterprise.models import ProjectFile
from docforge_enterprise.sharding import ShardPlan, shard_file


def test_python_ast_sharding_finds_symbols() -> None:
    content = "class A:\n    pass\n\ndef f():\n    return 1\n"
    file = ProjectFile(
        path=Path("x.py"),
        relative_path="x.py",
        language="python",
        kind="code",
        content=content,
        sha256="abc",
        size_bytes=len(content),
    )
    shards = shard_file(file, ShardPlan(max_chars=100, overlap=10))
    symbols = {symbol for shard in shards for symbol in shard.symbols}
    assert {"A", "f"}.issubset(symbols)


def test_typescript_symbol_sharding_finds_symbols() -> None:
    content = """
export class UserService {
  getUser(id: string) {
    return id;
  }
}

export const makeToken = async (value: string) => {
  return value;
};
"""
    file = ProjectFile(
        path=Path("service.ts"),
        relative_path="service.ts",
        language="typescript",
        kind="code",
        content=content,
        sha256="abc",
        size_bytes=len(content),
    )
    shards = shard_file(file, ShardPlan(max_chars=500, overlap=20))
    symbols = {symbol for shard in shards for symbol in shard.symbols}
    assert "UserService" in symbols
    assert "makeToken" in symbols


def test_java_symbol_sharding_finds_class_and_method() -> None:
    content = """
package demo;

public class AuthService {
    public String issueToken(String user) {
        return user;
    }
}
"""
    file = ProjectFile(
        path=Path("AuthService.java"),
        relative_path="AuthService.java",
        language="java",
        kind="code",
        content=content,
        sha256="abc",
        size_bytes=len(content),
    )
    shards = shard_file(file, ShardPlan(max_chars=500, overlap=20))
    symbols = {symbol for shard in shards for symbol in shard.symbols}
    assert "AuthService" in symbols
    assert "issueToken" in symbols
