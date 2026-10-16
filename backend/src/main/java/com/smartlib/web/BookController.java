package com.smartlib.web;

import java.util.List;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.ModelAttribute;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.smartlib.common.PageResult;
import com.smartlib.common.Result;
import com.smartlib.domain.dto.BookQuery;
import com.smartlib.domain.dto.Dtos.BookView;
import com.smartlib.service.BookService;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;

/**
 * 书目对外接口（读者端）。
 */
@RestController
@RequestMapping("/api/books")
@Tag(name = "书目检索", description = "检索、详情、热门榜、相似推荐")
public class BookController {

    private final BookService bookService;

    public BookController(BookService bookService) {
        this.bookService = bookService;
    }

    @GetMapping
    @Operation(summary = "检索书目", description = "支持关键词、分类、出版社、年份区间与仅看可借；排序字段走白名单")
    public Result<PageResult<BookView>> search(@ModelAttribute BookQuery query) {
        return Result.ok(bookService.search(query));
    }

    @GetMapping("/hot")
    @Operation(summary = "热门榜", description = "按近 90 天借阅次数排序")
    public Result<List<BookView>> hot(@RequestParam(defaultValue = "10") int limit) {
        return Result.ok(bookService.hot(limit));
    }

    @GetMapping("/{id}")
    @Operation(summary = "书目详情", description = "带布隆过滤与多级缓存；不存在的 id 会被前置拦截")
    public Result<BookView> detail(@PathVariable long id) {
        return Result.ok(bookService.getById(id));
    }

    @GetMapping("/{id}/similar")
    @Operation(summary = "相似书推荐")
    public Result<List<BookView>> similar(@PathVariable long id,
                                         @RequestParam(defaultValue = "6") int limit) {
        return Result.ok(bookService.similar(id, limit));
    }
}
