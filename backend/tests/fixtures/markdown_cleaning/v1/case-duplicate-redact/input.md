# Markdown 清洗基准1

同一段落

同一段落

邮箱: a@sample.org
电话: 13800138000
银行卡: 4111111111110006
身份证: 11010519491231002X
内网: 10.0.0.1

`13800138000 11010519491231002X 4111111111110006 a@sample.org 10.0.0.1` 在内联码中不应脱敏。

[受保护链接](https://files.example/13800138000/11010519491231002X/4111111111110006/a@sample.org/10.0.0.1)

普通显示 a@b.com 与 10.0.0.1 应脱敏。

<mailto:a@b.com>

<https://10.0.0.1/path>

[引用显示][safe-ref]

![受保护图片](https://images.example/13800138000/11010519491231002X/4111111111110006/a@sample.org/10.0.0.1.png)

<span data-phone="13800138000" data-id="11010519491231002X" data-card="4111111111110006" data-email="a@sample.org" data-ip="10.0.0.1"></span>

```text
13800138000 11010519491231002X 4111111111110006 a@sample.org 10.0.0.1
```

<div>
13800138000 11010519491231002X 4111111111110006 a@sample.org 10.0.0.1
</div>

| h1 | h2 |
| --- | --- |
| A | B |

- item one
- item one

[safe-ref]: https://a@b.com/foo(1)
