package com.smartlib.integration;

import static org.assertj.core.api.Assertions.assertThat;

import java.math.BigDecimal;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.HexFormat;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

import com.smartlib.domain.entity.Book;
import com.smartlib.domain.entity.BookStock;
import com.smartlib.mapper.BookMapper;
import com.smartlib.mapper.BookStockMapper;

/**
 * HTTP 层集成测试：公共 API 与 Agent 内部接口（含 HMAC 签名校验）。
 *
 * <p>刻意使用 JDK 自带的 {@code java.net.http.HttpClient}，而不是框架提供的测试客户端：
 * Spring Boot 4 移除了 {@code TestRestTemplate}，用标准 HTTP 客户端可以让测试
 * 完全不受框架测试工具类的包路径变动影响。断言用原始 JSON 字符串包含判断，
 * 同样是为了避开 Jackson 3 的节点 API 变更。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.DEFINED_PORT,
        properties = {"server.port=18081", "smartlib.rate-limit.enabled=false"})
@ActiveProfiles("test")
class HttpApiTest {

    private static final String BASE = "http://127.0.0.1:18081";
    private static final String SECRET = "dev-internal-secret-please-change";

    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5)).build();

    @Autowired
    private BookMapper bookMapper;
    @Autowired
    private BookStockMapper stockMapper;
    @Autowired
    private com.smartlib.service.BookService bookService;
    @Autowired
    private com.smartlib.entity.ReaderFixture readerFixture;

    @Test
    @DisplayName("公共 API：无需签名即可访问，返回统一响应体并带 traceId")
    void publicApiWorks() throws Exception {
        long bookId = newBook(2);
        HttpResponse<String> resp = get("/api/books/" + bookId, false);
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(resp.body()).contains("\"code\":0");
        assertThat(resp.body()).contains("\"traceId\"");
        // 服务端必须回写 traceId 响应头，供链路追踪串联
        assertThat(resp.headers().firstValue("X-Trace-Id")).isPresent();
    }

    @Test
    @DisplayName("公共 API：不存在的图书返回 404 与业务错误码 2002")
    void notFoundBookReturns404() throws Exception {
        HttpResponse<String> resp = get("/api/books/99999999", false);
        assertThat(resp.statusCode()).isEqualTo(404);
        assertThat(resp.body()).contains("2002");
    }

    @Test
    @DisplayName("内部接口：缺少签名头必须 401")
    void internalWithoutSignIsRejected() throws Exception {
        HttpResponse<String> resp = get("/internal/agent/tools/ping", false);
        assertThat(resp.statusCode()).isEqualTo(401);
        assertThat(resp.body()).contains("4003");
    }

    @Test
    @DisplayName("内部接口：签名错误必须 401（防伪造）")
    void internalWithWrongSignIsRejected() throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(BASE + "/internal/agent/tools/ping"))
                .header("X-Internal-Timestamp", String.valueOf(System.currentTimeMillis() / 1000))
                .header("X-Internal-Sign", "deadbeef")
                .timeout(Duration.ofSeconds(10)).GET().build();
        HttpResponse<String> resp = http.send(request, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(401);
        assertThat(resp.body()).contains("签名不匹配");
    }

    @Test
    @DisplayName("内部接口：过期时间戳必须 401（防重放）")
    void internalWithExpiredTimestampIsRejected() throws Exception {
        // 用一个 1 小时前的时间戳：即使签名完全正确也必须被拒
        long oldTs = System.currentTimeMillis() / 1000 - 3600;
        String path = "/internal/agent/tools/ping";
        String sign = sign(oldTs, "GET", path, "");
        HttpRequest request = HttpRequest.newBuilder(URI.create(BASE + path))
                .header("X-Internal-Timestamp", String.valueOf(oldTs))
                .header("X-Internal-Sign", sign)
                .timeout(Duration.ofSeconds(10)).GET().build();
        HttpResponse<String> resp = http.send(request, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(401);
        assertThat(resp.body()).contains("时间戳偏差");
    }

    @Test
    @DisplayName("内部接口：正确签名 + 时间戳在窗口内 → 放行，且 traceId 透传")
    void internalWithValidSignIsAccepted() throws Exception {
        long ts = System.currentTimeMillis() / 1000;
        String path = "/internal/agent/tools/ping";
        String sign = sign(ts, "GET", path, "");
        HttpRequest request = HttpRequest.newBuilder(URI.create(BASE + path + "?echo=hello"))
                .header("X-Internal-Timestamp", String.valueOf(ts))
                // 注意：签名用的 path 必须包含 query string，这里刻意验证这一约定
                .header("X-Internal-Sign", sign(ts, "GET", path + "?echo=hello", ""))
                .header("X-Trace-Id", "trace-from-agent-001")
                .timeout(Duration.ofSeconds(10)).GET().build();
        HttpResponse<String> resp = http.send(request, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(resp.body()).contains("trace-from-agent-001");
        assertThat(resp.body()).contains("\"ok\":true");
    }

    @Test
    @DisplayName("借书 API：缺少 Idempotency-Key 返回 400 与错误码 1002")
    void borrowWithoutIdempotencyKeyIsRejected() throws Exception {
        long writerId = readerFixture.newReader("TEACHER", 100);
        long bookId = newBook(2);
        String body = "{\"readerId\":" + writerId + ",\"bookId\":" + bookId + "}";
        HttpResponse<String> resp = post("/api/borrows", body, null);
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("1002");
    }

    @Test
    @DisplayName("借书 API：同一 Idempotency-Key 重复提交只扣一次库存")
    void borrowIsIdempotentOverHttp() throws Exception {
        long bookId = newBook(3);
        long readerId = readerFixture.newReader("POSTGRAD", 100);
        String body = "{\"readerId\":" + readerId + ",\"bookId\":" + bookId + "}";
        String key = "http-idem-" + System.nanoTime();

        HttpResponse<String> first = post("/api/borrows", body, key);
        assertThat(first.statusCode()).isEqualTo(200);
        assertThat(first.body()).contains("\"idempotentReplay\":false");

        HttpResponse<String> second = post("/api/borrows", body, key);
        assertThat(second.statusCode()).isEqualTo(200);
        assertThat(second.body()).contains("\"idempotentReplay\":true");

        assertThat(stockMapper.findByBookId(bookId).getAvailableCopies()).isEqualTo(2);
    }

    @Test
    @DisplayName("借书 API：库存不足返回 409 与错误码 3001（失败必须是错误状态而不是 200）")
    void borrowOutOfStockReturns409() throws Exception {
        long bookId = newBook(0);
        // 用新建的读者：种子数据里的读者 2 带有逾期记录，会先被"逾期禁借"规则拦下（错误码 2006），
        // 那样就测不到库存不足这条路径了
        long readerId = readerFixture.newReader("UNDERGRAD", 100);
        String body = "{\"readerId\":" + readerId + ",\"bookId\":" + bookId + "}";
        HttpResponse<String> resp = post("/api/borrows", body, "http-nostock-" + System.nanoTime());
        assertThat(resp.statusCode()).isEqualTo(409);
        assertThat(resp.body()).contains("3001");
    }

    @Test
    @DisplayName("管理接口：可查询库存策略与对账结果")
    void adminEndpointsWork() throws Exception {
        HttpResponse<String> strategy = get("/api/admin/stock-strategy", false);
        assertThat(strategy.statusCode()).isEqualTo(200);
        assertThat(strategy.body()).contains("optimistic-db");
        assertThat(strategy.body()).contains("pessimistic-db");

        HttpResponse<String> reconcile = get("/api/admin/reconcile", false);
        assertThat(reconcile.statusCode()).isEqualTo(200);
        // 核心断言：任何时刻对账都必须是干净的
        assertThat(reconcile.body()).contains("\"consistent\":true");
    }

    // ------------------------------------------------------------------
    // 工具方法
    // ------------------------------------------------------------------

    /** 与 InternalSignFilter 完全一致的签名算法，用于验证服务端实现正确。 */
    private String sign(long timestamp, String method, String pathWithQuery, String body) throws Exception {
        String canonical = timestamp + "\n" + method.toUpperCase() + "\n" + pathWithQuery + "\n" + body;
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(SECRET.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        return HexFormat.of().formatHex(mac.doFinal(canonical.getBytes(StandardCharsets.UTF_8)));
    }

    private HttpResponse<String> get(String path, boolean signed) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(BASE + path))
                .timeout(Duration.ofSeconds(10)).GET();
        return http.send(builder.build(), HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> post(String path, String body, String idemKey) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(BASE + path))
                .header("Content-Type", "application/json")
                .timeout(Duration.ofSeconds(10))
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8));
        if (idemKey != null) {
            builder.header("Idempotency-Key", idemKey);
        }
        return http.send(builder.build(), HttpResponse.BodyHandlers.ofString());
    }

    /**
     * 造一本测试用书。
     *
     * <p><b>必须走 {@code bookService.create} 而不是直接 insert</b>：
     * 直接写库会绕过布隆过滤器的登记，导致这本书的详情接口被判为"一定不存在"而返回 404。
     * 这正是布隆过滤器唯一的使用风险（假阴性），测试用它来固化"新增书目必须登记"这条约束。
     */
    private long newBook(int copies) {
        Book book = new Book();
        book.setIsbn("HTTP-" + System.nanoTime());
        book.setTitle("HTTP 测试用书");
        book.setAuthor("测试");
        book.setPrice(new BigDecimal("20.00"));
        bookService.create(book);

        BookStock stock = new BookStock();
        stock.setBookId(book.getId());
        stock.setTotalCopies(copies);
        stock.setAvailableCopies(copies);
        stock.setLockedCopies(0);
        stock.setVersion(0);
        stock.setUpdatedAt(java.time.LocalDateTime.now());
        stockMapper.insert(stock);
        stockMapper.syncBookTotalCopies(book.getId(), copies);
        return book.getId();
    }
}
