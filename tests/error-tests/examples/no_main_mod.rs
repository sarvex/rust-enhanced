/*BEGIN*/fn main() {
//          ^^^^WARN(>=1.41.0-beta,rust_syntax_checking_include_tests=True) function is never used
//          ^^^^NOTE(>=1.41.0-beta,rust_syntax_checking_include_tests=True) #[warn(dead_code)]
//       ^^^^^^^^^WARN(>=1.23.0,<1.41.0-beta,rust_syntax_checking_include_tests=True) function is never used
//       ^^^^^^^^^NOTE(>=1.23.0,<1.41.0-beta,rust_syntax_checking_include_tests=True) #[warn(dead_code)]
//       ^^^^^^^^^NOTE(>=1.63.0-beta,rust_syntax_checking_include_tests=False) here is a function named `main`
//       ^^^^^^^^^MSG(>=1.63.0-beta,rust_syntax_checking_include_tests=False) See Primary: no_main.rs
}/*END*/
// ~NOTE(rust_syntax_checking_include_tests=False,<1.63.0-beta OR <1.23.0,rust_syntax_checking_include_tests=True) here is a function named
// ~MSG(rust_syntax_checking_include_tests=False,<1.63.0-beta OR <1.23.0,rust_syntax_checking_include_tests=True) See Primary: no_main.rs
