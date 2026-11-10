import { describe, expect, it, vi } from 'vitest'

import { createDraftLoader } from './load'

/** 手动控制 resolve 时机，才能让「旧请求后到」这种时序稳定复现 */
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('草稿详情加载（A4）', () => {
  it('先后发两次加载：旧响应后到时不覆盖新草稿', async () => {
    const first = deferred<string>()
    const second = deferred<string>()
    const pending = [first, second]
    const fetchDetail = vi.fn((id: string) => {
      expect(id).toBeTruthy()
      return pending.shift()!.promise
    })
    const loader = createDraftLoader(fetchDetail)
    const applied: string[] = []

    const run = async (id: string): Promise<void> => {
      const result = await loader.load(id)
      if (result?.ok) applied.push(result.detail)
    }

    const oldLoad = run('draft-old')
    const newLoad = run('draft-new')

    second.resolve('新草稿')
    await newLoad
    // 旧响应晚到：必须被丢掉，否则页面上又变回旧草稿
    first.resolve('旧草稿')
    await oldLoad

    expect(applied).toEqual(['新草稿'])
  })

  it('cancel（切页/卸载）会 abort 在途请求，且它的结果不再算数', async () => {
    const gate = deferred<string>()
    const signals: AbortSignal[] = []
    const loader = createDraftLoader((_id, signal) => {
      signals.push(signal)
      return gate.promise
    })

    const pendingLoad = loader.load('draft-1')
    loader.cancel()

    expect(signals[0].aborted).toBe(true)
    gate.reject(new Error('aborted'))
    await expect(pendingLoad).resolves.toBeNull()
  })

  it('正常加载原样返回详情', async () => {
    const loader = createDraftLoader(async (id: string) => ({ id }))

    await expect(loader.load('draft-9')).resolves.toEqual({
      ok: true,
      detail: { id: 'draft-9' },
    })
  })
})
