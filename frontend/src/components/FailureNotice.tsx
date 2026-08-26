/**
 * 失败的分类呈现（AE6）。
 *
 * 按 reason 而非 message 文本分类：message 会随后端文案调整而变，reason 是稳定契约。每类给出
 * 不同的下一步提示——统一显示「失败」会让用户不知道该改地址、换仓库还是重试。
 *
 * **首页与 Overview 页共用一份。** 提交被拒时（地址非法、限流、凭证缺失）用户还在首页，
 * 那一刻还没有 taskId 可以跳转；分析中途失败时用户在 Overview 页。两处的呈现必须一致，
 * 各写一份会让新增的 reason 只在一处生效。
 */

import { Link } from 'react-router-dom'
import { SETTINGS } from '../routes/paths'

export const FAILURE_HINTS: Record<string, string> = {
  invalid_url: '地址格式不正确。请用 https://github.com/owner/repo 的形式。',
  not_found: '仓库不存在或不可访问。若是私有仓库，需要配置 GITHUB_TOKEN 才能区分这两种情况。',
  no_access: '没有访问权限。私有仓库需要有权限的 token。',
  too_large: '仓库规模超出本系统的处理上限。可以换一个较小的仓库。',
  // 克隆侧已自动重试三次（见 backend/ingest/clone.py），所以走到这里说明重试也没救回来。
  // 提示直接提交而非「稍后」：网络类失败会退还限流配额，用户可以立刻再试。
  network_error: '网络异常，且自动重试未成功。可以直接再提交一次——这次失败不占用限流配额。',
  // 与上一条的区别是请求有没有到达后端。client_offline 时没有克隆、没有重试、没有配额消耗，
  // 挂上「已重试三次、不占配额」会把人指向错误的下一步（去重试而非去看后端是否在跑）。
  // 两者共用一个 reason 曾导致这个误导，所以在契约层就分开，而不是靠 message 文本区分。
  client_offline: '无法连接后端服务。确认后端已启动，以及地址与端口是否正确。',
  analysis_failed: '分析在中途失败。上面的说明来自后端，可据此判断是重试还是换仓库。',
  task_not_found: '任务不存在，可能已因服务重启而丢失。请重新提交。',
  credentials_required: '需要先在设置页填写自己的 LLM 凭证（base_url 与 API key）。',
  rate_limited: '提交过于频繁。等待提示中给出的时间后再试。',
  queue_full: '当前排队已满，稍后再提交。',
  disk_quota: '服务器磁盘配额不足，稍后再试。',
}

interface Props {
  reason: string
  message: string
}

export function FailureNotice({ reason, message }: Props) {
  return (
    <div className="failure" role="alert">
      <h2 className="failure__title">分析未能完成</h2>
      <p className="failure__message">{message}</p>
      {FAILURE_HINTS[reason] ? (
        <p className="failure__hint">{FAILURE_HINTS[reason]}</p>
      ) : null}
      {reason === 'credentials_required' ? (
        <Link className="failure__link" to={SETTINGS}>
          去设置页填写凭证
        </Link>
      ) : null}
    </div>
  )
}
