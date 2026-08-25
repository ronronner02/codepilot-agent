/**
 * 仓库地址输入。
 *
 * 用原生 `<form>` 而非按钮加 onClick：回车提交是免费得到的，而手写 keydown 处理容易漏掉
 * 输入法回车、数字小键盘回车这类情形。
 *
 * 错误信息用 aria-describedby 与输入框关联，读屏才会在聚焦输入框时播报它——只把错误文本
 * 放在旁边的话，读屏用户听不到它与哪个字段有关。
 */

import { useId, useState } from 'react'

interface Props {
  disabled: boolean
  /** 分析中时的提示。与 disabled 分开：禁用的原因要能说出来，否则用户不知道为什么点不动。 */
  disabledReason?: string
  onSubmit: (repoUrl: string) => void
}

export function RepoInput({ disabled, disabledReason, onSubmit }: Props) {
  const [value, setValue] = useState('')
  const [localError, setLocalError] = useState('')
  const inputId = useId()
  const errorId = useId()
  const hintId = useId()

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    const trimmed = value.trim()
    if (!trimmed) {
      // 本地校验拦住空提交：让一个立即可判断的错误立即可见，而不是往返一次服务端。
      setLocalError('请输入 GitHub 仓库地址')
      return
    }
    setLocalError('')
    onSubmit(trimmed)
  }

  return (
    <form className="repo-input" onSubmit={handleSubmit} noValidate>
      <label className="repo-input__label" htmlFor={inputId}>
        GitHub 仓库地址
      </label>
      <div className="repo-input__row">
        <input
          id={inputId}
          className="repo-input__field"
          type="text"
          value={value}
          placeholder="https://github.com/fastapi/fastapi"
          onChange={(event) => {
            setValue(event.target.value)
            if (localError) setLocalError('')
          }}
          disabled={disabled}
          aria-invalid={localError ? true : undefined}
          aria-describedby={localError ? errorId : hintId}
        />
        <button className="repo-input__submit" type="submit" disabled={disabled}>
          {disabled ? '分析中…' : '开始分析'}
        </button>
      </div>
      {localError ? (
        // role="alert" 让读屏立即播报，而不必等用户把焦点移回输入框。
        <p className="repo-input__error" id={errorId} role="alert">
          {localError}
        </p>
      ) : (
        <p className="repo-input__hint" id={hintId}>
          {disabled && disabledReason
            ? disabledReason
            : '支持公开仓库。分析包含架构报告、代码评审与可追问的索引。'}
        </p>
      )}
    </form>
  )
}
