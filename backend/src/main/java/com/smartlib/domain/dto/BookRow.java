package com.smartlib.domain.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 书目联合查询读模型（book + book_stock + category 的一次性投影）。
 *
 * <p>本类由 scripts/tools/gen_entities.py 生成，请勿手工修改字段：
 * 需要调整字段请修改生成器中的字段清单后重新生成（与
 * backend/src/main/resources/db/schema.sql 一一对应）。
 */
public class BookRow {

    private Long id;
    private String isbn;
    private String title;
    private String author;
    private String publisher;
    private String categoryName;
    private String categoryCode;
    private Integer publishYear;
    private BigDecimal price;
    private String tags;
    private String summary;
    private String shelfLocation;
    private Integer totalCopies;
    private Integer availableCopies;
    private Double hotScore;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getIsbn() {
        return isbn;
    }

    public void setIsbn(String isbn) {
        this.isbn = isbn;
    }

    public String getTitle() {
        return title;
    }

    public void setTitle(String title) {
        this.title = title;
    }

    public String getAuthor() {
        return author;
    }

    public void setAuthor(String author) {
        this.author = author;
    }

    public String getPublisher() {
        return publisher;
    }

    public void setPublisher(String publisher) {
        this.publisher = publisher;
    }

    public String getCategoryName() {
        return categoryName;
    }

    public void setCategoryName(String categoryName) {
        this.categoryName = categoryName;
    }

    public String getCategoryCode() {
        return categoryCode;
    }

    public void setCategoryCode(String categoryCode) {
        this.categoryCode = categoryCode;
    }

    public Integer getPublishYear() {
        return publishYear;
    }

    public void setPublishYear(Integer publishYear) {
        this.publishYear = publishYear;
    }

    public BigDecimal getPrice() {
        return price;
    }

    public void setPrice(BigDecimal price) {
        this.price = price;
    }

    public String getTags() {
        return tags;
    }

    public void setTags(String tags) {
        this.tags = tags;
    }

    public String getSummary() {
        return summary;
    }

    public void setSummary(String summary) {
        this.summary = summary;
    }

    public String getShelfLocation() {
        return shelfLocation;
    }

    public void setShelfLocation(String shelfLocation) {
        this.shelfLocation = shelfLocation;
    }

    public Integer getTotalCopies() {
        return totalCopies;
    }

    public void setTotalCopies(Integer totalCopies) {
        this.totalCopies = totalCopies;
    }

    public Integer getAvailableCopies() {
        return availableCopies;
    }

    public void setAvailableCopies(Integer availableCopies) {
        this.availableCopies = availableCopies;
    }

    public Double getHotScore() {
        return hotScore;
    }

    public void setHotScore(Double hotScore) {
        this.hotScore = hotScore;
    }

    @Override
    public String toString() {
        return "BookRow{"
                + "id=" + id + ", "
                + "isbn=" + isbn + ", "
                + "title=" + title + ", "
                + "author=" + author + ", "
                + "publisher=" + publisher + ", "
                + "categoryName=" + categoryName + ", "
                + "categoryCode=" + categoryCode + ", "
                + "publishYear=" + publishYear + ", "
                + "price=" + price + ", "
                + "tags=" + tags + ", "
                + "summary=" + summary + ", "
                + "shelfLocation=" + shelfLocation + ", "
                + "totalCopies=" + totalCopies + ", "
                + "availableCopies=" + availableCopies + ", "
                + "hotScore=" + hotScore + ", "
                + '}';
    }
}
