"""HTTP 状态到拒绝原因的映射。

这是 AE6（拉取失败可区分原因）的实现点：界面要按原因分别呈现，不能统一报「失败」。
映射错了的后果不是崩溃，而是用户看到错误的指引——把私有仓库说成不存在，用户会去
检查自己有没有拼错地址。

纯函数，不需要网络。fetch_metadata 的网络路径由基准仓库实跑覆盖。
"""

from __future__ import annotations

from backend.ingest.github_api import build_headers, map_error_status
from backend.ingest.guards import RejectReason, RepoRef
from tests.support import make_settings

REF = RepoRef(owner="acme", name="widget")


class TestNotFound:
    def test_404_authenticated_says_not_found(self) -> None:
        """带 token 时 404 是确定的「不存在」。"""
        rejected = map_error_status(404, REF, authenticated=True)
        assert rejected.reason is RejectReason.NOT_FOUND
        assert "不存在" in str(rejected)
        assert "私有" not in str(rejected)

    def test_404_unauthenticated_admits_ambiguity(self) -> None:
        """无 token 时 GitHub 对私有与不存在都返回 404，文案必须诚实。

        这是 GitHub 的有意设计：若私有仓库返回 403，就能枚举出「哪些私有仓库存在」。
        文案把「不存在」说成确定结论会误导用户去检查地址拼写。
        """
        rejected = map_error_status(404, REF, authenticated=False)
        assert rejected.reason is RejectReason.NOT_FOUND
        message = str(rejected)
        assert "私有" in message
        assert "GITHUB_TOKEN" in message, "应告知用户如何区分这两种情况"

    def test_404_message_carries_repo_slug(self) -> None:
        assert "acme/widget" in str(map_error_status(404, REF, authenticated=True))


class TestNoAccess:
    def test_403_maps_to_no_access(self) -> None:
        rejected = map_error_status(403, REF, authenticated=True)
        assert rejected.reason is RejectReason.NO_ACCESS

    def test_403_message_mentions_rate_limit_possibility(self) -> None:
        """403 既可能是无权限也可能是速率超限，文案不应只说一种。"""
        assert "速率" in str(map_error_status(403, REF, authenticated=True))

    def test_451_maps_to_no_access(self) -> None:
        """451 是法律原因下架，属于「访问不到」而非「不存在」。"""
        rejected = map_error_status(451, REF, authenticated=False)
        assert rejected.reason is RejectReason.NO_ACCESS
        assert "法律" in str(rejected)


class TestOtherStatuses:
    def test_500_maps_to_network_error(self) -> None:
        rejected = map_error_status(500, REF, authenticated=True)
        assert rejected.reason is RejectReason.NETWORK_ERROR
        assert "500" in str(rejected)

    def test_429_maps_to_network_error_not_no_access(self) -> None:
        """429 未单独处理，落进兜底。断言当前行为，便于日后有意改动时可见。"""
        assert map_error_status(429, REF, authenticated=True).reason is RejectReason.NETWORK_ERROR

    def test_unexpected_status_still_produces_actionable_message(self) -> None:
        message = str(map_error_status(418, REF, authenticated=False))
        assert "418" in message
        assert "acme/widget" in message


class TestHeaders:
    def test_token_absent_omits_authorization(self) -> None:
        headers = build_headers(make_settings(github_token=""))
        assert "Authorization" not in headers

    def test_token_present_sets_bearer(self) -> None:
        headers = build_headers(make_settings(github_token="ghp_example"))
        assert headers["Authorization"] == "Bearer ghp_example"

    def test_api_version_pinned(self) -> None:
        """不钉版本会让 GitHub 的 API 演进悄悄改变响应结构。"""
        assert build_headers(make_settings())["X-GitHub-Api-Version"] == "2022-11-28"
