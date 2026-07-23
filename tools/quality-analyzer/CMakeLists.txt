cmake_minimum_required(VERSION 3.16)
project(hls_quality_analyzer VERSION 1.0.0 LANGUAGES CXX)

add_executable(hls-quality-analyzer src/main.cpp)
target_compile_features(hls-quality-analyzer PRIVATE cxx_std_17)
target_compile_options(hls-quality-analyzer PRIVATE
  $<$<CXX_COMPILER_ID:GNU,Clang,AppleClang>:-Wall;-Wextra;-Wpedantic;-Wconversion>
)

find_package(Threads REQUIRED)
target_link_libraries(hls-quality-analyzer PRIVATE Threads::Threads)

install(TARGETS hls-quality-analyzer RUNTIME DESTINATION bin)
