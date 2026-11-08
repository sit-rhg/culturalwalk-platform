/**
 * 草稿详情的加载协调。
 *
 * 两个坑都在这里收口：
 * - 切草稿/切页时旧请求可能后到，晚到的响应必须丢掉，否则页面闪回上一个草稿（请求序号）；
 * - 组件卸载后不该继续占着网络，所以在途请求随新加载或 cancel() 一起 abort。
 * 抽成模块而不是写在组件里，是因为页面测试只有 SSR，跑不到这些时序分支。
 */

export type DraftLoadResult<T> = { ok: true; detail: T } | { ok: false; error: unknown }

export function createDraftLoader<T>(
  fetchDetail: (id: string, signal: AbortSignal) => Promise<T>,
): {
  /** 返回 null = 这次加载已被更新的加载（或 cancel）取代，调用方整个丢掉即可 */
  load: (id: string) => Promise<DraftLoadResult<T> | null>
  cancel: () => void
} {
  let seq = 0
  let current: AbortController | null = null

  return {
    async load(id) {
      current?.abort()
      const controller = new AbortController()
      current = controller
      const mine = ++seq
      try {
        const detail = await fetchDetail(id, controller.signal)
        return mine === seq ? { ok: true, detail } : null
      } catch (error) {
        return mine === seq ? { ok: false, error } : null
      }
    },
    cancel() {
      seq += 1
      current?.abort()
      current = null
    },
  }
}
