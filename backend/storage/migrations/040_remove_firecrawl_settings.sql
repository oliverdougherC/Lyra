-- Exa is the only built-in web-research provider. Keep the source-content toggle,
-- but remove the obsolete loopback service URL and provider-specific column name.
alter table settings rename column firecrawl_scrape_enabled to source_content_enabled;
alter table settings drop column firecrawl_base_url;
