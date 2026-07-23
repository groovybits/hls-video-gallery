cmake_minimum_required(VERSION 3.16)
project(hls_quality_analyzer VERSION 1.1.1 LANGUAGES CXX)

add_executable(hls-quality-analyzer src/main.cpp)
target_compile_features(hls-quality-analyzer PRIVATE cxx_std_17)
target_compile_options(hls-quality-analyzer PRIVATE
  $<$<CXX_COMPILER_ID:GNU,Clang,AppleClang>:-Wall;-Wextra;-Wpedantic;-Wconversion>
)

add_executable(hls-quality-report-renderer src/report_renderer_main.cpp)
target_compile_features(hls-quality-report-renderer PRIVATE cxx_std_17)
target_compile_options(hls-quality-report-renderer PRIVATE
  $<$<CXX_COMPILER_ID:GNU,Clang,AppleClang>:-Wall;-Wextra;-Wpedantic;-Wconversion>
)

find_package(Threads REQUIRED)
target_link_libraries(hls-quality-analyzer PRIVATE Threads::Threads)

install(
  TARGETS hls-quality-analyzer hls-quality-report-renderer
  RUNTIME DESTINATION bin
)
