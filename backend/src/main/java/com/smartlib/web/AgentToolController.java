package com.smartlib.web;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.smartlib.common.BizException;
import com.smartlib.common.ErrorCode;
import com.smartlib.common.Result;
import com.smartlib.domain.dto.BookQuery;
import com.smartlib.domain.dto.Dtos.BorrowResult;
import com.smartlib.domain.dto.Dtos.BookView;
import com.smartlib.domain.dto.Dtos.LoanView;
import com.smartlib.domain.dto.Dtos.ReservationResult;
import com.smartlib.domain.dto.Dtos.SearchResult;
import com.smartlib.domain.dto.Dtos.StockView;
import com.smartlib.service.BookSearchService;
import com.smartlib.service.BookService;
import com.smartlib.service.BorrowService;
import com.smartlib.service.ReaderService;
import com.smartlib.service.ReservationService;
import com.smartlib.service.RuleEngine;
import com.smartlib.service.StockService;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;

/**
 * Agent 工具接口（内部）。
 *
 * <p><b>为什么单独开一组 {@code /internal/agent/**} 而不是让 Agent 直接调公共 API</b>：
 * <ol>
 *   <li><b>权限边界</b>：公共 API 面向读者端（未来是登录态），
 *       内部接口用 HMAC 签名，两者的信任模型不同，混在一起会模糊边界；</li>
 *   <li><b>限流隔离</b>：Agent 的异常重试不应该消耗读者端的配额（限流 key 不同）；</li>
 *   <li><b>契约稳定</b>：这 8 个接口是给 Agent 用的"工具契约"，
 *       可以被版本化、被单独压测，而公共 API 会随着产品迭代频繁调整；</li>
 *   <li><b>可观测</b>：Agent 调用量、失败率可以单独打点，
 *       否则"是人在用还是机器在用"就分不清了。</li>
 * </ol>
 *
 * <p><b>写操作必须带幂等键</b>：Agent 会重试（网络抖动、模型重试），
 * 没有幂等键的话"借书"这类写操作会被重复执行。
 */
@RestController
@RequestMapping("/internal/agent/tools")
@Tag(name = "Agent 工具", description = "供 Python Agent 服务调用的内部接口，需 HMAC 签名")
public class AgentToolController {

    private final BookSearchService bookSearchService;
    private final BookService bookService;
    private final StockService stockService;
    private final BorrowService borrowService;
    private final ReaderService readerService;
    private final ReservationService reservationService;
    private final RuleEngine ruleEngine;

    public AgentToolController(BookSearchService bookSearchService,
                               BookService bookService,
                               StockService stockService,
                               BorrowService borrowService,
                               ReaderService readerService,
                               ReservationService reservationService,
                               RuleEngine ruleEngine) {
        this.bookSearchService = bookSearchService;
        this.bookService = bookService;
        this.stockService = stockService;
        this.borrowService = borrowService;
        this.readerService = readerService;
        this.reservationService = reservationService;
        this.ruleEngine = ruleEngine;
    }

    // --------------------------- 1. 检索书目 ---------------------------

    public record SearchBooksRequest(String query, String category, Integer page, Integer size,
                                     String sortBy, Boolean availableOnly) {
    }

    @PostMapping("/search-books")
    @Operation(summary = "工具：检索书目")
    public Result<SearchResult> searchBooks(@RequestBody(required = false) SearchBooksRequest request) {
        SearchBooksRequest req = request == null
                ? new SearchBooksRequest(null, null, null, null, null, null) : request;
        BookQuery query = new BookQuery();
        query.setQuery(req.query());
        query.setCategory(req.category());
        query.setPage(req.page() == null ? 1 : req.page());
        query.setSize(req.size() == null ? 5 : req.size());
        query.setSort(req.sortBy() == null ? "relevance" : req.sortBy());
        query.setAvailableOnly(req.availableOnly());
        // 让 Agent 拿到的记录里带上简介与馆藏位置，减少"每个候选再查一次详情"的往返
        return Result.ok(bookSearchService.search(query));
    }

    // --------------------------- 2. 查询库存 ---------------------------

    @GetMapping("/books/{id}/stock")
    @Operation(summary = "工具：查询实时库存",
            description = "availableCopies = -1 表示库存暂时无法确认（降级态），不是 0")
    public Result<StockView> stock(@PathVariable long id) {
        BookView book = bookService.getById(id);
        StockView stock = stockService.view(id);
        return Result.ok(new StockView(id, book.title(), stock.totalCopies(), stock.availableCopies(),
                stock.queueLen(), book.shelfLocation()));
    }

    // --------------------------- 3. 借书 ---------------------------

    public record BorrowToolRequest(@NotNull Long readerId, @NotNull Long bookId, String idempotencyKey) {
    }

    @PostMapping("/borrow")
    @Operation(summary = "工具：借书", description = "需要幂等键，Agent 重试不会重复扣库存")
    public Result<BorrowResult> borrow(@Valid @RequestBody BorrowToolRequest request) {
        String idemKey = request.idempotencyKey();
        if (idemKey == null || idemKey.isBlank()) {
            // 没有幂等键就替 Agent 生成一个不会重复的键：
            // 这样即使 Agent 侧漏传，也不会因为重试造成重复借书
            idemKey = "agent-auto-" + request.readerId() + "-" + request.bookId() + "-" + System.nanoTime();
        }
        BorrowResult result = borrowService.borrow(request.readerId(), request.bookId(), idemKey);
        if (!result.ok()) {
            // 与公共 API 保持一致：失败用 HTTP 状态码 + 错误码表达。
            // Agent 侧据此生成"这本书暂时借不到"的话术，而不是误报成功。
            throw new BizException(ErrorCode.STOCK_NOT_ENOUGH, result.reason());
        }
        return Result.ok(result);
    }

    // --------------------------- 4. 续借 ---------------------------

    public record RenewToolRequest(@NotNull Long readerId, @NotNull Long borrowId) {
    }

    @PostMapping("/renew")
    @Operation(summary = "工具：续借")
    public Result<BorrowResult> renew(@Valid @RequestBody RenewToolRequest request) {
        return Result.ok(borrowService.renew(request.readerId(), request.borrowId()));
    }

    // --------------------------- 5. 我的借阅 ---------------------------

    @GetMapping("/readers/{id}/loans")
    @Operation(summary = "工具：查询读者的在借记录")
    public Result<List<LoanView>> loans(@PathVariable long id,
                                        @RequestParam(defaultValue = "20") int limit) {
        return Result.ok(readerService.myLoans(id, limit));
    }

    // --------------------------- 6. 读者画像 ---------------------------

    @GetMapping("/readers/{id}/profile")
    @Operation(summary = "工具：查询读者画像", description = "推荐与规则问答的上下文")
    public Result<Map<String, Object>> profile(@PathVariable long id) {
        return Result.ok(readerService.profile(id));
    }

    // --------------------------- 7. 预约 ---------------------------

    public record ReserveToolRequest(@NotNull Long readerId, @NotNull Long bookId) {
    }

    @PostMapping("/reservations")
    @Operation(summary = "工具：预约图书")
    public Result<ReservationResult> reserve(@Valid @RequestBody ReserveToolRequest request) {
        return Result.ok(reservationService.reserve(request.readerId(), request.bookId()));
    }

    // --------------------------- 8. 规则/政策问答 ---------------------------

    /**
     * 规则问答（结构化知识源）。
     *
     * <p><b>这个接口是"反幻觉"的关键设计</b>：借阅册数、借期、罚金标准这类问题，
     * 答案必须来自 {@code rule_config} 表的真实配置，而不是让大模型凭记忆回答。
     * 大模型对"你们学校本科生能借几本书"这类私有事实必然是编的，
     * 因此这类问题在 Agent 侧被强制路由到本接口。
     *
     * @param topic      问题主题关键词，如"能借几本""逾期罚金""怎么续借"
     * @param readerType 读者类型，用于给出该类型的具体数值
     */
    @GetMapping("/policy")
    @Operation(summary = "工具：规则/政策问答", description = "答案来自 rule_config 表，不依赖模型记忆")
    public Result<Map<String, Object>> policy(@RequestParam @NotBlank String topic,
                                              @RequestParam(required = false) String readerType) {
        String type = readerType == null || readerType.isBlank() ? "UNDERGRAD" : readerType;
        var rule = ruleEngine.ruleFor(type);
        String normalized = topic.toLowerCase();

        List<String> answers = new ArrayList<>();
        if (containsAny(normalized, "几本", "册数", "能借", "上限", "额度")) {
            answers.add(String.format("%s 类型读者最多可同时借阅 %d 册。", typeName(type), rule.maxBorrowCount()));
        }
        if (containsAny(normalized, "多久", "期限", "几天", "借期")) {
            answers.add(String.format("借期为 %d 天，可续借 %d 次，每次 %d 天（单册最长借阅 %d 天）。",
                    rule.loanDays(), rule.maxRenewCount(), rule.renewDays(), rule.maxRenewDays()));
        }
        if (containsAny(normalized, "续借", "延期")) {
            answers.add(String.format("续借规则：最多续借 %d 次，每次延长 %d 天；"
                            + "如果该书已被其他读者预约，则不能续借，请按时归还。",
                    rule.maxRenewCount(), rule.renewDays()));
        }
        if (containsAny(normalized, "逾期", "罚金", "罚款", "超期")) {
            answers.add(String.format("逾期罚金为每册每天 %s 元；欠费超过 %s 元将无法继续借阅，请先缴清。",
                    rule.finePerDay().toPlainString(), rule.fineFreeLimit().toPlainString()));
        }
        if (containsAny(normalized, "预约", "排队", "占位")) {
            answers.add(String.format("预约规则：无可借复本时可预约，队列按先后顺序；"
                            + "书到馆后为你保留 %d 天，逾期未取会自动顺延给下一位。",
                    rule.reservationHoldDays()));
        }
        if (containsAny(normalized, "信用分", "信用")) {
            answers.add(String.format("信用分初始 100，逾期 1 次扣 5 分，预约到馆未取扣 3 分，按时归还加 1 分；"
                            + "低于 %d 分将暂时无法借阅。", rule.minCreditScore()));
        }
        if (answers.isEmpty()) {
            answers.add("这个问题我暂时没有对应的规则条目。你可以换个说法，"
                    + "或直接咨询图书馆服务台（工作日 8:30-17:30）。");
        }

        Map<String, Object> data = new LinkedHashMap<>();
        data.put("topic", topic);
        data.put("readerType", type);
        data.put("answer", String.join("\n", answers));
        data.put("source", "rule_config");
        data.put("ruleSnapshot", Map.of(
                "maxBorrowCount", rule.maxBorrowCount(),
                "loanDays", rule.loanDays(),
                "maxRenewCount", rule.maxRenewCount(),
                "renewDays", rule.renewDays(),
                "finePerDay", rule.finePerDay().toPlainString(),
                "reservationHoldDays", rule.reservationHoldDays()));
        return Result.ok(data);
    }

    private boolean containsAny(String text, String... keywords) {
        for (String keyword : keywords) {
            if (text.contains(keyword)) {
                return true;
            }
        }
        return false;
    }

    private String typeName(String type) {
        return switch (type) {
            case "UNDERGRAD" -> "本科生";
            case "POSTGRAD" -> "研究生";
            case "TEACHER" -> "教师";
            default -> type;
        };
    }

    /** 供联调排查：不查数据库，直接回显签名与 trace 是否正确透传。 */
    @GetMapping("/ping")
    @Operation(summary = "工具：连通性与签名自检")
    public Result<Map<String, Object>> ping(@RequestParam(required = false) String echo) {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("ok", true);
        data.put("echo", echo);
        data.put("traceId", com.smartlib.common.TraceContext.get());
        data.put("serverTime", java.time.LocalDateTime.now().toString());
        data.put("stockStrategy", "见 /api/admin/stock-strategy");
        return Result.ok(data);
    }

    /** 参数越界时的显式保护（防止 Agent 传入超大 size 打爆内存）。 */
    private void guard(long value, String name) {
        if (value <= 0 || value > 1_000_000) {
            throw new BizException(ErrorCode.PARAM_INVALID, name + " 超出允许范围：" + value);
        }
    }
}
