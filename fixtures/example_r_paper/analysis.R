# Minimal R analysis fixture for repro-check CI (base R only).
set.seed(1)
x <- rnorm(100); y <- 2*x + rnorm(100)
fit <- lm(y ~ x)
cat("slope:", round(coef(fit)[["x"]], 3), "\n")
cat("n:", length(x), "\n")
