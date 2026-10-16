-- =============================================================================
-- SmartLib 库存原子预扣脚本（Redis + Lua）
--
-- 为什么必须用 Lua：GET 判断 + DECRBY 是两步操作，如果用客户端分两次发命令，
-- 两个并发请求可能同时读到 available=1 然后各自 DECRBY，结果变成 -1。
-- Redis 单线程执行 Lua 脚本，整个脚本是一个原子操作，从根上消除这个竞态。
--
-- KEYS[1] = 库存 key，形如 smartlib:stock:{bookId}
-- ARGV[1] = 本次扣减数量
--
-- 返回值约定：
--   >= 0  扣减成功，返回扣减后的剩余库存
--   -1    key 不存在，需要先从数据库预热（调用方回源 DB 写回 Redis 后重试一次）
--   -2    库存不足，直接失败
-- =============================================================================
local key = KEYS[1]
local qty = tonumber(ARGV[1])

local current = redis.call('GET', key)
if not current then
    return -1
end

current = tonumber(current)
if current < qty then
    return -2
end

return redis.call('DECRBY', key, qty)
