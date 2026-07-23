#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <mutex>
#include <numeric>
#include <optional>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <vector>

#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

namespace fs = std::filesystem;

namespace {

constexpr const char* kAnalyzerName = "hls-quality-analyzer";
constexpr const char* kAnalyzerVersion = "1.0.0";
constexpr const char* kConfigVersion = "quality-composite-v1";
constexpr std::size_t kHashWidth = 32;
constexpr std::size_t kHashHeight = 32;
constexpr std::size_t kPairedHashFrameBytes = kHashWidth * kHashHeight * 2;
constexpr double kPi = 3.14159265358979323846;

struct Error : std::runtime_error {
    using std::runtime_error::runtime_error;
};

std::string trim(std::string value) {
    const auto first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    const auto last = value.find_last_not_of(" \t\r\n");
    return value.substr(first, last - first + 1);
}

std::string lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return value;
}

double parse_double(const std::string& value, double fallback = 0.0) {
    try {
        std::size_t consumed = 0;
        const double result = std::stod(value, &consumed);
        if (consumed == 0 || !std::isfinite(result)) return fallback;
        return result;
    } catch (...) {
        return fallback;
    }
}

long long parse_integer(const std::string& value, long long fallback = 0) {
    try {
        std::size_t consumed = 0;
        const long long result = std::stoll(value, &consumed);
        return consumed ? result : fallback;
    } catch (...) {
        return fallback;
    }
}

double parse_fraction(const std::string& value) {
    const auto slash = value.find('/');
    if (slash == std::string::npos) return parse_double(value);
    const double numerator = parse_double(value.substr(0, slash));
    const double denominator = parse_double(value.substr(slash + 1));
    return denominator == 0.0 ? 0.0 : numerator / denominator;
}

double clamp_score(double value) {
    return std::max(0.0, std::min(100.0, value));
}

std::string json_escape(const std::string& value) {
    std::ostringstream out;
    for (char raw_character : value) {
        const auto c = static_cast<unsigned char>(raw_character);
        switch (c) {
            case '"': out << "\\\""; break;
            case '\\': out << "\\\\"; break;
            case '\b': out << "\\b"; break;
            case '\f': out << "\\f"; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default:
                if (c < 0x20) {
                    out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                        << static_cast<int>(c) << std::dec << std::setfill(' ');
                } else {
                    out << static_cast<char>(c);
                }
        }
    }
    return out.str();
}

std::string html_escape(const std::string& value) {
    std::string out;
    out.reserve(value.size());
    for (char c : value) {
        switch (c) {
            case '&': out += "&amp;"; break;
            case '<': out += "&lt;"; break;
            case '>': out += "&gt;"; break;
            case '"': out += "&quot;"; break;
            case '\'': out += "&#39;"; break;
            default: out += c;
        }
    }
    return out;
}

std::string utc_now() {
    const std::time_t now = std::time(nullptr);
    std::tm tm{};
#if defined(_WIN32)
    gmtime_s(&tm, &now);
#else
    gmtime_r(&now, &tm);
#endif
    std::ostringstream out;
    out << std::put_time(&tm, "%Y-%m-%dT%H:%M:%SZ");
    return out.str();
}

std::string format_number(double value, int precision = 3) {
    if (!std::isfinite(value)) return "null";
    std::ostringstream out;
    out << std::fixed << std::setprecision(precision) << value;
    return out.str();
}

std::string optional_number(const std::optional<double>& value, int precision = 3) {
    return value ? format_number(*value, precision) : "null";
}

void atomic_write(const fs::path& destination, const std::string& contents) {
    const fs::path parent =
        destination.parent_path().empty() ? fs::path(".") : destination.parent_path();
    if (!destination.parent_path().empty()) fs::create_directories(parent);

    std::string template_path =
        (parent / ("." + destination.filename().string() + ".XXXXXX")).string();
    std::vector<char> template_buffer(template_path.begin(), template_path.end());
    template_buffer.push_back('\0');
    int file_descriptor = ::mkstemp(template_buffer.data());
    if (file_descriptor < 0) {
        throw Error(
            "Cannot exclusively create temporary output for " + destination.string() +
            ": " + std::strerror(errno));
    }
    const fs::path temporary(template_buffer.data());

    const auto fail_before_publish = [&](const std::string& operation) {
        const int saved_errno = errno;
        if (file_descriptor >= 0) {
            ::close(file_descriptor);
            file_descriptor = -1;
        }
        ::unlink(temporary.c_str());
        throw Error(
            operation + " " + temporary.string() + ": " + std::strerror(saved_errno));
    };

    if (::fchmod(file_descriptor, S_IRUSR | S_IWUSR) != 0) {
        fail_before_publish("Cannot secure temporary output");
    }
    std::size_t written = 0;
    while (written < contents.size()) {
        const ssize_t result =
            ::write(file_descriptor, contents.data() + written, contents.size() - written);
        if (result < 0 && errno == EINTR) continue;
        if (result <= 0) {
            if (result == 0) errno = EIO;
            fail_before_publish("Cannot write temporary output");
        }
        written += static_cast<std::size_t>(result);
    }
    while (::fsync(file_descriptor) != 0) {
        if (errno == EINTR) continue;
        fail_before_publish("Cannot sync temporary output");
    }
    if (::close(file_descriptor) != 0) {
        const int saved_errno = errno;
        file_descriptor = -1;
        ::unlink(temporary.c_str());
        throw Error(
            "Cannot close temporary output " + temporary.string() + ": " +
            std::strerror(saved_errno));
    }
    file_descriptor = -1;

    if (::rename(temporary.c_str(), destination.c_str()) != 0) {
        const int saved_errno = errno;
        ::unlink(temporary.c_str());
        throw Error(
            "Cannot publish " + destination.string() + ": " + std::strerror(saved_errno));
    }

    int directory_flags = O_RDONLY;
#ifdef O_CLOEXEC
    directory_flags |= O_CLOEXEC;
#endif
#ifdef O_DIRECTORY
    directory_flags |= O_DIRECTORY;
#endif
    const int directory_descriptor = ::open(parent.c_str(), directory_flags);
    if (directory_descriptor >= 0) {
        while (::fsync(directory_descriptor) != 0 && errno == EINTR) {
        }
        ::close(directory_descriptor);
    }
}

struct ProcessResult {
    int exit_code = -1;
    std::string standard_output;
    std::string standard_error;
};

std::string command_for_display(const std::vector<std::string>& arguments) {
    std::ostringstream out;
    bool first = true;
    for (const auto& argument : arguments) {
        if (!first) out << ' ';
        first = false;
        if (!argument.empty() &&
            argument.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:=+,-") ==
                std::string::npos) {
            out << argument;
        } else {
            out << '\'';
            for (char c : argument) {
                if (c == '\'') out << "'\\''";
                else out << c;
            }
            out << '\'';
        }
    }
    return out.str();
}

ProcessResult run_process(
    const std::vector<std::string>& arguments,
    const std::function<void(const char*, std::size_t)>& stdout_consumer = {},
    std::size_t capture_limit = 4 * 1024 * 1024
) {
    if (arguments.empty()) throw Error("Attempted to run an empty command");

    int stdout_pipe[2]{};
    int stderr_pipe[2]{};
    if (::pipe(stdout_pipe) != 0 || ::pipe(stderr_pipe) != 0) {
        throw Error("Unable to create process pipes: " + std::string(std::strerror(errno)));
    }

    const pid_t child = ::fork();
    if (child < 0) {
        ::close(stdout_pipe[0]); ::close(stdout_pipe[1]);
        ::close(stderr_pipe[0]); ::close(stderr_pipe[1]);
        throw Error("Unable to fork: " + std::string(std::strerror(errno)));
    }
    if (child == 0) {
        ::dup2(stdout_pipe[1], STDOUT_FILENO);
        ::dup2(stderr_pipe[1], STDERR_FILENO);
        ::close(stdout_pipe[0]); ::close(stdout_pipe[1]);
        ::close(stderr_pipe[0]); ::close(stderr_pipe[1]);

        std::vector<char*> argv;
        argv.reserve(arguments.size() + 1);
        for (const auto& argument : arguments) argv.push_back(const_cast<char*>(argument.c_str()));
        argv.push_back(nullptr);
        ::execvp(argv[0], argv.data());
        const std::string message = "execvp failed for " + arguments.front() + ": " +
            std::string(std::strerror(errno)) + "\n";
        ::write(STDERR_FILENO, message.data(), message.size());
        ::_exit(127);
    }

    ::close(stdout_pipe[1]);
    ::close(stderr_pipe[1]);

    ProcessResult result;
    std::exception_ptr callback_error;
    std::mutex callback_mutex;

    std::thread stdout_reader([&] {
        std::array<char, 64 * 1024> buffer{};
        while (true) {
            const ssize_t count = ::read(stdout_pipe[0], buffer.data(), buffer.size());
            if (count == 0) break;
            if (count < 0) {
                if (errno == EINTR) continue;
                break;
            }
            try {
                if (stdout_consumer) {
                    stdout_consumer(buffer.data(), static_cast<std::size_t>(count));
                } else if (result.standard_output.size() < capture_limit) {
                    const std::size_t available = capture_limit - result.standard_output.size();
                    result.standard_output.append(buffer.data(),
                        std::min(available, static_cast<std::size_t>(count)));
                }
            } catch (...) {
                std::lock_guard<std::mutex> guard(callback_mutex);
                callback_error = std::current_exception();
            }
        }
        ::close(stdout_pipe[0]);
    });

    std::thread stderr_reader([&] {
        std::array<char, 16 * 1024> buffer{};
        while (true) {
            const ssize_t count = ::read(stderr_pipe[0], buffer.data(), buffer.size());
            if (count == 0) break;
            if (count < 0) {
                if (errno == EINTR) continue;
                break;
            }
            if (result.standard_error.size() < capture_limit) {
                const std::size_t available = capture_limit - result.standard_error.size();
                result.standard_error.append(buffer.data(),
                    std::min(available, static_cast<std::size_t>(count)));
            }
        }
        ::close(stderr_pipe[0]);
    });

    int status = 0;
    while (::waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) break;
    }
    stdout_reader.join();
    stderr_reader.join();
    if (WIFEXITED(status)) result.exit_code = WEXITSTATUS(status);
    else if (WIFSIGNALED(status)) result.exit_code = 128 + WTERMSIG(status);

    if (callback_error) std::rethrow_exception(callback_error);
    return result;
}

std::string error_tail(const std::string& value, std::size_t lines = 16) {
    std::vector<std::string> all;
    std::istringstream input(value);
    std::string line;
    while (std::getline(input, line)) all.push_back(line);
    const std::size_t start = all.size() > lines ? all.size() - lines : 0;
    std::ostringstream out;
    for (std::size_t index = start; index < all.size(); ++index) {
        if (index != start) out << '\n';
        out << all[index];
    }
    return out.str();
}

struct Options {
    fs::path reference;
    fs::path distorted;
    fs::path output_directory;
    std::optional<fs::path> progress_json;
    int threads = 2;
    double frame_rate = 30.0;
    double scene_threshold = 10.0;
    double min_scene_seconds = 2.0;
};

void print_usage(std::ostream& output) {
    output
        << "Usage: " << kAnalyzerName << " --reference FILE --distorted FILE --output-dir DIR [options]\n"
        << "\nOptions:\n"
        << "  --threads N              FFmpeg/libvmaf threads (default: 2)\n"
        << "  --frame-rate N           Aligned analysis frames per second (default: 30)\n"
        << "  --scene-threshold N      FFmpeg reference-scene threshold, 0-100 (default: 10)\n"
        << "  --min-scene-seconds N    Merge shorter scenes (default: 2)\n"
        << "  --progress-json PATH     Atomically publish live machine-readable progress\n"
        << "  --version                Show analyzer version\n"
        << "  --help                   Show this help\n";
}

Options parse_arguments(int argc, char** argv) {
    Options options;
    bool have_reference = false;
    bool have_distorted = false;
    bool have_output = false;
    for (int index = 1; index < argc; ++index) {
        const std::string name = argv[index];
        if (name == "--help" || name == "-h") {
            print_usage(std::cout);
            std::exit(0);
        }
        if (name == "--version") {
            std::cout << kAnalyzerName << ' ' << kAnalyzerVersion << "\n";
            std::exit(0);
        }
        if (index + 1 >= argc) throw Error("Missing value for " + name);
        const std::string value = argv[++index];
        if (name == "--reference") {
            options.reference = value;
            have_reference = true;
        } else if (name == "--distorted") {
            options.distorted = value;
            have_distorted = true;
        } else if (name == "--output-dir") {
            options.output_directory = value;
            have_output = true;
        } else if (name == "--progress-json") {
            options.progress_json = fs::path(value);
        } else if (name == "--threads") {
            options.threads = static_cast<int>(parse_integer(value, -1));
        } else if (name == "--frame-rate") {
            options.frame_rate = parse_double(value, -1.0);
        } else if (name == "--scene-threshold") {
            options.scene_threshold = parse_double(value, -1.0);
        } else if (name == "--min-scene-seconds") {
            options.min_scene_seconds = parse_double(value, -1.0);
        } else {
            throw Error("Unknown option " + name);
        }
    }
    if (!have_reference || !have_distorted || !have_output) {
        throw Error("--reference, --distorted, and --output-dir are required");
    }
    if (options.threads < 1 || options.threads > 64) {
        throw Error("--threads must be between 1 and 64");
    }
    if (options.frame_rate <= 0.0 || options.frame_rate > 120.0) {
        throw Error("--frame-rate must be greater than 0 and no more than 120");
    }
    if (options.scene_threshold < 0.0 || options.scene_threshold > 100.0) {
        throw Error("--scene-threshold must be between 0 and 100");
    }
    if (options.min_scene_seconds <= 0.0 || options.min_scene_seconds > 3600.0) {
        throw Error("--min-scene-seconds must be greater than 0 and no more than 3600");
    }
    return options;
}

struct VideoProbe {
    int width = 0;
    int height = 0;
    int rotation = 0;
    double duration = 0.0;
    double frame_rate = 0.0;
    std::string color_space = "unknown";
    std::string color_transfer = "unknown";
    std::string color_primaries = "unknown";
    std::string color_range = "unknown";
};

std::string unquote_flat_value(std::string value) {
    value = trim(value);
    if (value.size() >= 2 && value.front() == '"' && value.back() == '"') {
        value = value.substr(1, value.size() - 2);
        std::string decoded;
        for (std::size_t index = 0; index < value.size(); ++index) {
            if (value[index] == '\\' && index + 1 < value.size()) decoded += value[++index];
            else decoded += value[index];
        }
        return decoded;
    }
    return value;
}

VideoProbe probe_video(const fs::path& path) {
    const std::vector<std::string> command = {
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate,color_space,color_transfer,"
        "color_primaries,color_range,duration:stream_tags=rotate:stream_side_data=rotation:format=duration",
        "-of", "flat", path.string()
    };
    const auto result = run_process(command);
    if (result.exit_code != 0) {
        throw Error("ffprobe failed for " + path.string() + ":\n" + error_tail(result.standard_error));
    }

    VideoProbe probe;
    double stream_duration = 0.0;
    double format_duration = 0.0;
    std::istringstream input(result.standard_output);
    std::string line;
    while (std::getline(input, line)) {
        const auto equals = line.find('=');
        if (equals == std::string::npos) continue;
        const std::string key = line.substr(0, equals);
        const std::string value = unquote_flat_value(line.substr(equals + 1));
        if (key == "streams.stream.0.width") probe.width = static_cast<int>(parse_integer(value));
        else if (key == "streams.stream.0.height") probe.height = static_cast<int>(parse_integer(value));
        else if (key == "streams.stream.0.avg_frame_rate") probe.frame_rate = parse_fraction(value);
        else if (key == "streams.stream.0.r_frame_rate" && probe.frame_rate <= 0.0)
            probe.frame_rate = parse_fraction(value);
        else if (key == "streams.stream.0.color_space") probe.color_space = lower(value);
        else if (key == "streams.stream.0.color_transfer") probe.color_transfer = lower(value);
        else if (key == "streams.stream.0.color_primaries") probe.color_primaries = lower(value);
        else if (key == "streams.stream.0.color_range") probe.color_range = lower(value);
        else if (key == "streams.stream.0.duration") stream_duration = parse_double(value);
        else if (key == "format.duration") format_duration = parse_double(value);
        else if (key.find("rotation") != std::string::npos || key.find("tags.rotate") != std::string::npos)
            probe.rotation = static_cast<int>(parse_integer(value));
    }
    probe.rotation %= 360;
    if (probe.rotation < 0) probe.rotation += 360;
    if (probe.rotation == 90 || probe.rotation == 270) std::swap(probe.width, probe.height);
    probe.duration = format_duration > 0.0 ? format_duration : stream_duration;
    if (probe.width <= 0 || probe.height <= 0 || probe.duration <= 0.0) {
        throw Error("ffprobe did not return usable dimensions and duration for " + path.string());
    }
    return probe;
}

bool ffmpeg_has_filter(const std::string& filter) {
    const auto result = run_process({"ffmpeg", "-hide_banner", "-filters"});
    if (result.exit_code != 0) return false;
    const std::regex pattern("(^|\\n)[^\\n]*\\s" + filter + "\\s");
    return std::regex_search(result.standard_output + result.standard_error, pattern);
}

int ffmpeg_major_version() {
    const auto result = run_process({"ffmpeg", "-version"});
    const std::regex version_pattern(R"(ffmpeg version\s+([0-9]+))");
    std::smatch match;
    const std::string text = result.standard_output + result.standard_error;
    if (result.exit_code == 0 && std::regex_search(text, match, version_pattern)) {
        return static_cast<int>(parse_integer(match[1].str()));
    }
    return 0;
}

bool is_hdr_transfer(const std::string& transfer) {
    const std::string value = lower(transfer);
    return value == "smpte2084" || value == "arib-std-b67" || value == "hlg" ||
           value == "pq";
}

struct Normalization {
    std::string reference_filter;
    std::string distorted_filter;
    std::string reference_description;
    std::string distorted_description;
    bool uses_hdr_tonemap = false;
};

std::string zscale_primaries(std::string value) {
    value = lower(value);
    if (value == "bt2020") return "2020";
    if (value == "smpte432" || value == "display-p3") return "smpte432";
    return "709";
}

std::string zscale_matrix(std::string value) {
    value = lower(value);
    if (value == "bt2020nc" || value == "bt2020ncl") return "2020_ncl";
    return "709";
}

std::string colorspace_space(std::string value) {
    value = lower(value);
    static const std::vector<std::string> supported = {
        "bt709", "fcc", "bt470bg", "smpte170m", "smpte240m", "ycgco", "gbr", "bt2020nc"
    };
    return std::find(supported.begin(), supported.end(), value) != supported.end()
        ? value : "bt709";
}

std::string colorspace_primaries(std::string value) {
    value = lower(value);
    static const std::vector<std::string> supported = {
        "bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "smpte428",
        "film", "smpte431", "smpte432", "bt2020", "jedec-p22"
    };
    return std::find(supported.begin(), supported.end(), value) != supported.end()
        ? value : "bt709";
}

std::string colorspace_transfer(std::string value) {
    value = lower(value);
    static const std::vector<std::string> supported = {
        "bt709", "bt470m", "gamma22", "bt470bg", "gamma28", "smpte170m",
        "smpte240m", "linear", "srgb", "iec61966-2-1", "xvycc",
        "iec61966-2-4", "bt2020-10", "bt2020-12"
    };
    return std::find(supported.begin(), supported.end(), value) != supported.end()
        ? value : "bt709";
}

std::string pair_display_normalization_filter(const VideoProbe& reference, bool has_zscale,
                                              bool has_tonemap,
                                              std::string& description) {
    if (is_hdr_transfer(reference.color_transfer)) {
        if (!has_zscale || !has_tonemap) {
            throw Error(
                "HDR/HLG input requires FFmpeg's zscale and tonemap filters "
                "for common BT.709 display normalization"
            );
        }
        const std::string transfer = reference.color_transfer == "hlg"
            ? "arib-std-b67" : reference.color_transfer == "pq"
                ? "smpte2084" : reference.color_transfer;
        const std::string range =
            reference.color_range == "pc" || reference.color_range == "jpeg" ? "full" : "limited";
        description =
            "Reference-derived HDR/HLG input interpretation, linear light, Mobius tone map, "
            "BT.709 TV-range display";
        return "zscale=pin=" + zscale_primaries(reference.color_primaries) +
               ":tin=" + transfer + ":min=" + zscale_matrix(reference.color_space) +
               ":rin=" + range + ":t=linear:npl=100,format=gbrpf32le,"
               "zscale=p=bt709,tonemap=tonemap=mobius:desat=2,"
               "zscale=t=bt709:m=bt709:r=tv,format=yuv420p";
    }
    const std::string input_space = colorspace_space(reference.color_space);
    const std::string input_primaries = colorspace_primaries(reference.color_primaries);
    const std::string input_transfer = colorspace_transfer(reference.color_transfer);
    const std::string input_range =
        reference.color_range == "pc" || reference.color_range == "jpeg" ? "pc" : "tv";
    description =
        "Reference-derived SDR interpretation (" + input_space + "/" + input_primaries +
        "/" + input_transfer + "/" + input_range +
        "), color-managed to BT.709 TV-range display";
    return "colorspace=ispace=" + input_space + ":iprimaries=" + input_primaries +
           ":itrc=" + input_transfer + ":irange=" + input_range +
           ":space=bt709:primaries=bt709:trc=bt709:range=tv:format=yuv420p";
}

Normalization build_normalization(const VideoProbe& reference) {
    if (!ffmpeg_has_filter("libvmaf")) {
        throw Error("FFmpeg does not provide the required libvmaf filter");
    }
    if (!ffmpeg_has_filter("colorspace")) {
        throw Error("FFmpeg does not provide the required colorspace filter");
    }
    if (!ffmpeg_has_filter("scdet")) {
        throw Error("FFmpeg does not provide the required scdet filter");
    }
    const bool has_zscale = ffmpeg_has_filter("zscale");
    const bool has_tonemap = ffmpeg_has_filter("tonemap");
    Normalization result;
    result.reference_filter =
        pair_display_normalization_filter(
            reference, has_zscale, has_tonemap, result.reference_description
        );
    result.distorted_filter = result.reference_filter;
    result.distorted_description = result.reference_description;
    result.uses_hdr_tonemap =
        is_hdr_transfer(reference.color_transfer);
    return result;
}

std::string filter_quote(const fs::path& path) {
    std::string out = "'";
    for (char c : path.string()) {
        if (c == '\\' || c == '\'') out += '\\';
        out += c;
    }
    out += "'";
    return out;
}

std::string common_input_filter(const std::string& normalization, int width, int height,
                                double frame_rate) {
    std::ostringstream filter;
    filter << "settb=AVTB,setpts=PTS-STARTPTS,fps=fps=" << std::fixed
           << std::setprecision(6) << frame_rate << ":start_time=0:round=near:eof_action=round"
           << ',' << normalization
           << ",scale=w=" << width << ":h=" << height << ":flags=bicubic,setsar=1";
    return filter.str();
}

std::vector<std::string> sanitize_arguments(
    const std::vector<std::string>& arguments,
    const Options& options
) {
    std::vector<std::string> sanitized;
    sanitized.reserve(arguments.size());
    const std::string output_root = fs::absolute(options.output_directory).lexically_normal().string();
    const auto replace_all = [](std::string value, const std::string& needle,
                                const std::string& replacement) {
        if (needle.empty()) return value;
        std::size_t position = 0;
        while ((position = value.find(needle, position)) != std::string::npos) {
            value.replace(position, needle.size(), replacement);
            position += replacement.size();
        }
        return value;
    };
    for (const auto& argument : arguments) {
        if (argument == options.reference.string()) sanitized.emplace_back("$REFERENCE");
        else if (argument == options.distorted.string()) sanitized.emplace_back("$DISTORTED");
        else {
            std::string safe = replace_all(argument, output_root, "$OUTPUT_DIR");
            safe = replace_all(safe, options.reference.string(), "$REFERENCE");
            safe = replace_all(safe, options.distorted.string(), "$DISTORTED");
            sanitized.push_back(std::move(safe));
        }
    }
    return sanitized;
}

class ProgressWriter {
public:
    ProgressWriter(const Options& options, double duration, std::size_t total_frames)
        : options_(options), duration_(duration), total_frames_(total_frames),
          started_(std::chrono::steady_clock::now()) {}

    void update(const std::string& phase, double processed_seconds, double percent,
                std::size_t frames_done, std::size_t scenes_detected,
                const std::vector<std::string>& command, double fps, double speed,
                double eta_seconds, bool active = true, const std::string& error = {},
                bool force = false) {
        if (!options_.progress_json) return;
        const auto now = std::chrono::steady_clock::now();
        if (!force && now - last_write_ < std::chrono::milliseconds(300)) return;
        last_write_ = now;
        const double elapsed =
            std::chrono::duration_cast<std::chrono::duration<double>>(now - started_).count();
        const auto sanitized = sanitize_arguments(command, options_);
        std::ostringstream json;
        json << "{\n"
             << "  \"schema_version\": 1,\n"
             << "  \"analyzer_version\": \"" << kAnalyzerVersion << "\",\n"
             << "  \"config_version\": \"" << kConfigVersion << "\",\n"
             << "  \"active\": " << (active ? "true" : "false") << ",\n"
             << "  \"phase\": \"" << json_escape(phase) << "\",\n"
             << "  \"elapsed_seconds\": " << format_number(elapsed, 3) << ",\n"
             << "  \"processed_seconds\": " << format_number(std::max(0.0, processed_seconds), 3) << ",\n"
             << "  \"duration_seconds\": " << format_number(duration_, 3) << ",\n"
             << "  \"percent\": " << format_number(clamp_score(percent), 3) << ",\n"
             << "  \"fps\": " << format_number(std::max(0.0, fps), 3) << ",\n"
             << "  \"speed\": " << format_number(std::max(0.0, speed), 3) << ",\n"
             << "  \"eta_seconds\": " << format_number(std::max(0.0, eta_seconds), 3) << ",\n"
             << "  \"frames_done\": " << frames_done << ",\n"
             << "  \"frames_total\": " << total_frames_ << ",\n"
             << "  \"total_frames\": " << total_frames_ << ",\n"
             << "  \"scenes_detected\": " << scenes_detected << ",\n"
             << "  \"ffmpeg_command\": \"" << json_escape(command_for_display(sanitized)) << "\",\n"
             << "  \"ffmpeg_args\": [";
        for (std::size_t index = 0; index < sanitized.size(); ++index) {
            if (index) json << ", ";
            json << "\"" << json_escape(sanitized[index]) << "\"";
        }
        json << "],\n"
             << "  \"updated_at\": \"" << utc_now() << "\",\n"
             << "  \"error\": \"" << json_escape(error) << "\"\n"
             << "}\n";
        atomic_write(*options_.progress_json, json.str());
    }

private:
    const Options& options_;
    double duration_;
    std::size_t total_frames_;
    std::chrono::steady_clock::time_point started_;
    std::chrono::steady_clock::time_point last_write_{};
};

std::uint64_t perceptual_hash(const std::uint8_t* image) {
    static const std::array<std::array<double, kHashWidth>, 8> cosine = [] {
        std::array<std::array<double, kHashWidth>, 8> table{};
        for (std::size_t frequency = 0; frequency < 8; ++frequency) {
            for (std::size_t position = 0; position < kHashWidth; ++position) {
                table[frequency][position] =
                    std::cos((2.0 * static_cast<double>(position) + 1.0) *
                             static_cast<double>(frequency) * kPi /
                             (2.0 * static_cast<double>(kHashWidth)));
            }
        }
        return table;
    }();

    std::array<double, 64> coefficients{};
    for (std::size_t v = 0; v < 8; ++v) {
        for (std::size_t u = 0; u < 8; ++u) {
            double sum = 0.0;
            for (std::size_t y = 0; y < kHashHeight; ++y) {
                for (std::size_t x = 0; x < kHashWidth; ++x) {
                    sum += static_cast<double>(image[y * kHashWidth + x]) *
                           cosine[u][x] * cosine[v][y];
                }
            }
            coefficients[v * 8 + u] = sum;
        }
    }
    std::array<double, 63> ac{};
    std::copy(coefficients.begin() + 1, coefficients.end(), ac.begin());
    std::nth_element(ac.begin(), ac.begin() + static_cast<std::ptrdiff_t>(ac.size() / 2), ac.end());
    const double median = ac[ac.size() / 2];
    std::uint64_t hash = 0;
    for (std::size_t index = 0; index < coefficients.size(); ++index) {
        if (coefficients[index] >= median) hash |= (std::uint64_t{1} << index);
    }
    return hash;
}

unsigned hamming_distance(std::uint64_t left, std::uint64_t right) {
#if defined(__GNUC__) || defined(__clang__)
    return static_cast<unsigned>(__builtin_popcountll(left ^ right));
#else
    std::uint64_t value = left ^ right;
    unsigned count = 0;
    while (value) {
        value &= value - 1;
        ++count;
    }
    return count;
#endif
}

double hash_similarity(std::uint64_t left, std::uint64_t right) {
    return 100.0 * (1.0 - static_cast<double>(hamming_distance(left, right)) / 64.0);
}

std::pair<std::uint64_t, std::uint64_t> paired_perceptual_hashes(
    const std::uint8_t* paired_image
) {
    std::array<std::uint8_t, kHashWidth * kHashHeight> reference{};
    std::array<std::uint8_t, kHashWidth * kHashHeight> distorted{};
    for (std::size_t row = 0; row < kHashHeight; ++row) {
        const std::uint8_t* source = paired_image + row * kHashWidth * 2;
        std::copy_n(source, kHashWidth, reference.data() + row * kHashWidth);
        std::copy_n(source + kHashWidth, kHashWidth, distorted.data() + row * kHashWidth);
    }
    return {perceptual_hash(reference.data()), perceptual_hash(distorted.data())};
}

struct HashFrame {
    std::uint64_t reference = 0;
    std::uint64_t distorted = 0;
    double similarity = 0.0;
    double reference_change = 0.0;
    double distorted_change = 0.0;
    double temporal_consistency = 100.0;
};

struct VmafFrame {
    int frame = -1;
    std::optional<double> standard;
    std::optional<double> phone;
    std::optional<double> psnr_y;
    std::optional<double> ssim;
};

std::vector<VmafFrame> parse_vmaf_log(const fs::path& path) {
    std::ifstream input(path);
    if (!input) throw Error("Cannot open libvmaf log " + path.string());
    const std::regex frame_pattern(R"("frameNum"\s*:\s*([0-9]+))");
    const std::regex metric_pattern(
        R"REGEX("([A-Za-z0-9_]+)"\s*:\s*(-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?))REGEX");
    std::vector<VmafFrame> frames;
    std::string line;
    int current_frame = -1;
    bool metrics = false;
    VmafFrame current;
    while (std::getline(input, line)) {
        std::smatch match;
        if (std::regex_search(line, match, frame_pattern)) {
            if (current_frame >= 0) frames.push_back(current);
            current_frame = static_cast<int>(parse_integer(match[1].str(), -1));
            current = VmafFrame{};
            current.frame = current_frame;
            metrics = false;
            continue;
        }
        if (current_frame < 0) continue;
        if (line.find("\"metrics\"") != std::string::npos) {
            metrics = true;
            continue;
        }
        if (!metrics) continue;
        if (line.find('}') != std::string::npos && line.find(':') == std::string::npos) {
            frames.push_back(current);
            current_frame = -1;
            metrics = false;
            continue;
        }
        if (!std::regex_search(line, match, metric_pattern)) continue;
        const std::string name = match[1].str();
        const double value = parse_double(match[2].str(), std::numeric_limits<double>::quiet_NaN());
        if (!std::isfinite(value)) continue;
        if (name == "standard" || name == "vmaf" || name == "vmaf_standard")
            current.standard = value;
        else if (name == "phone" || name == "vmaf_phone") current.phone = value;
        else if (name == "psnr_y") current.psnr_y = value;
        else if (name == "float_ssim" || name == "ssim") current.ssim = value;
    }
    if (current_frame >= 0) frames.push_back(current);
    return frames;
}

std::vector<double> parse_scene_log(const fs::path& path, std::size_t frame_count) {
    std::ifstream input(path);
    if (!input) throw Error("Cannot open FFmpeg scene log " + path.string());
    const std::regex frame_pattern(R"(frame:([0-9]+))");
    const std::regex score_pattern(
        R"(lavfi\.scd\.score=(-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?))");
    std::vector<double> scores(frame_count, 0.0);
    std::string line;
    std::size_t frame = 0;
    bool have_frame = false;
    while (std::getline(input, line)) {
        std::smatch match;
        if (std::regex_search(line, match, frame_pattern)) {
            frame = static_cast<std::size_t>(std::max<long long>(
                0, parse_integer(match[1].str())));
            have_frame = true;
        } else if (have_frame && frame < scores.size() &&
                   std::regex_search(line, match, score_pattern)) {
            scores[frame] = parse_double(match[1].str());
        }
    }
    return scores;
}

struct CombinedRun {
    std::vector<HashFrame> hashes;
    std::vector<VmafFrame> vmaf_frames;
    std::vector<double> scene_scores;
    bool phone_available = false;
    std::vector<std::string> warnings;
};

std::vector<std::string> combined_command(
    const Options& options, const VideoProbe& distorted, const Normalization& normalization,
    const fs::path& vmaf_log, const fs::path& scene_log, bool phone,
    bool legacy_nested_escaping, bool legacy_autorotate_syntax
) {
    const std::string reference_filter =
        common_input_filter(normalization.reference_filter, distorted.width, distorted.height,
                            options.frame_rate);
    const std::string distorted_filter =
        common_input_filter(normalization.distorted_filter, distorted.width, distorted.height,
                            options.frame_rate);
    // libavfilter 8 performs two parsing passes over nested model options. Two
    // runtime backslashes keep each model colon intact on FFmpeg 5.1 while
    // remaining accepted by newer FFmpeg/libvmaf builds.
    const std::string nested_colon = legacy_nested_escaping ? "\\\\:" : "\\:";
    const std::string models = phone
        ? "version=vmaf_v0.6.1" + nested_colon + "name=vmaf_standard|"
          "version=vmaf_v0.6.1" + nested_colon + "name=vmaf_phone" +
          nested_colon + "enable_transform=true"
        : "version=vmaf_v0.6.1" + nested_colon + "name=vmaf_standard";
    const std::string graph =
        "[0:v]" + reference_filter + "[reference_base];" +
        "[1:v]" + distorted_filter + "[distorted_base];" +
        "[reference_base]split=3[reference_vmaf][reference_hash][reference_scene];" +
        "[distorted_base]split=2[distorted_vmaf][distorted_hash];" +
        "[distorted_vmaf][reference_vmaf]libvmaf=log_fmt=json:log_path=" +
        filter_quote(vmaf_log) + ":n_threads=" + std::to_string(options.threads) +
        ":model='" + models + "':feature='name=psnr|name=float_ssim'"
        ":eof_action=endall:shortest=1:repeatlast=0[vmaf_done];" +
        "[vmaf_done]nullsink;" +
        "[reference_scene]scdet=threshold=" + format_number(options.scene_threshold, 6) +
        ",metadata=mode=print:key=lavfi.scd.score:file=" + filter_quote(scene_log) +
        "[scene_done];[scene_done]nullsink;" +
        "[reference_hash]scale=32:32:flags=area,format=gray[reference_small];" +
        "[distorted_hash]scale=32:32:flags=area,format=gray[distorted_small];" +
        "[reference_small][distorted_small]hstack=inputs=2:shortest=1[out]";
    std::vector<std::string> command = {
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-xerror",
    };
    const auto append_input = [&](const fs::path& input) {
        command.insert(command.end(), {"-fflags", "+genpts", "-autorotate"});
        // FFmpeg 5.x requires the explicit boolean value while newer releases
        // use the flag form. Autorotation remains enabled for both aligned
        // inputs so phone footage is compared in its displayed orientation.
        if (legacy_autorotate_syntax) command.emplace_back("1");
        command.insert(command.end(), {
            "-threads", std::to_string(options.threads), "-i", input.string()
        });
    };
    append_input(options.reference);
    append_input(options.distorted);
    command.insert(command.end(), {
        "-filter_complex_threads", std::to_string(options.threads),
        "-filter_complex", graph, "-map", "[out]", "-an", "-sn", "-dn",
        "-pix_fmt", "gray", "-f", "rawvideo", "-"
    });
    return command;
}

CombinedRun analyze_combined(
    const Options& options, const VideoProbe& distorted, const Normalization& normalization,
    ProgressWriter& progress, double common_duration, std::size_t expected_frames
) {
    const fs::path output_root = fs::absolute(options.output_directory);
    const fs::path vmaf_log = output_root / ".libvmaf-analysis.json.tmp";
    const fs::path scene_log = output_root / ".scene-analysis.txt.tmp";
    std::error_code ignored;

    const int ffmpeg_major = ffmpeg_major_version();
    const bool preferred_legacy_escaping = ffmpeg_major > 0 && ffmpeg_major <= 5;
    const bool legacy_autorotate_syntax = ffmpeg_major > 0 && ffmpeg_major <= 5;
    auto execute = [&](bool phone, bool legacy_nested_escaping) {
        fs::remove(vmaf_log, ignored);
        fs::remove(scene_log, ignored);
        const auto command =
            combined_command(options, distorted, normalization, vmaf_log, scene_log, phone,
                             legacy_nested_escaping, legacy_autorotate_syntax);
        std::vector<HashFrame> hashes;
        hashes.reserve(expected_frames);
        std::vector<std::uint8_t> pending;
        pending.reserve(kPairedHashFrameBytes * 2);
        const auto started = std::chrono::steady_clock::now();
        const auto result = run_process(command, [&](const char* data, std::size_t size) {
            const auto* bytes = reinterpret_cast<const std::uint8_t*>(data);
            pending.insert(pending.end(), bytes, bytes + size);
            std::size_t offset = 0;
            while (pending.size() - offset >= kPairedHashFrameBytes) {
                const auto [reference_hash, distorted_hash] =
                    paired_perceptual_hashes(pending.data() + offset);
                HashFrame frame;
                frame.reference = reference_hash;
                frame.distorted = distorted_hash;
                frame.similarity = hash_similarity(reference_hash, distorted_hash);
                if (!hashes.empty()) {
                    frame.reference_change =
                        100.0 - hash_similarity(hashes.back().reference, reference_hash);
                    frame.distorted_change =
                        100.0 - hash_similarity(hashes.back().distorted, distorted_hash);
                    frame.temporal_consistency = clamp_score(
                        100.0 - std::abs(frame.reference_change - frame.distorted_change));
                }
                hashes.push_back(frame);
                offset += kPairedHashFrameBytes;

                const auto now = std::chrono::steady_clock::now();
                const double elapsed = std::max(
                    0.001, std::chrono::duration_cast<std::chrono::duration<double>>(
                               now - started).count());
                const double processed = static_cast<double>(hashes.size()) / options.frame_rate;
                const double speed = processed / elapsed;
                const double eta = speed > 0.0
                    ? std::max(0.0, common_duration - processed) / speed : 0.0;
                progress.update("quality_metrics", processed,
                    common_duration > 0.0 ? 95.0 * processed / common_duration : 0.0,
                    hashes.size(), 0, command, static_cast<double>(hashes.size()) / elapsed,
                    speed, eta);
            }
            if (offset) {
                pending.erase(pending.begin(),
                    pending.begin() + static_cast<std::ptrdiff_t>(offset));
            }
        });
        if (!pending.empty() && result.exit_code == 0) {
            throw Error("FFmpeg returned a truncated paired pHash frame");
        }
        return std::make_tuple(result, command, hashes);
    };

    CombinedRun output;
    auto [result, command, hashes] = execute(true, preferred_legacy_escaping);
    if (result.exit_code != 0) {
        std::tie(result, command, hashes) = execute(true, !preferred_legacy_escaping);
    }
    output.phone_available = result.exit_code == 0;
    if (result.exit_code != 0) {
        output.warnings.emplace_back(
            "Phone VMAF model was unavailable; it is omitted from informational metrics.");
        std::tie(result, command, hashes) = execute(false, preferred_legacy_escaping);
        if (result.exit_code != 0) {
            std::tie(result, command, hashes) = execute(false, !preferred_legacy_escaping);
        }
    }
    if (result.exit_code != 0) {
        fs::remove(vmaf_log, ignored);
        fs::remove(scene_log, ignored);
        throw Error("FFmpeg combined quality analysis failed:\n" +
                    error_tail(result.standard_error));
    }
    output.hashes = std::move(hashes);
    output.vmaf_frames = parse_vmaf_log(vmaf_log);
    output.scene_scores = parse_scene_log(scene_log, output.hashes.size());
    fs::remove(vmaf_log, ignored);
    fs::remove(scene_log, ignored);

    if (output.hashes.empty() || output.vmaf_frames.empty()) {
        throw Error("Combined FFmpeg analysis produced no aligned metric frames");
    }
    for (const auto& frame : output.vmaf_frames) {
        if (!frame.standard || !frame.psnr_y || !frame.ssim) {
            throw Error("libvmaf output is missing standard VMAF, PSNR, or SSIM features");
        }
    }
    if (output.phone_available &&
        std::none_of(output.vmaf_frames.begin(), output.vmaf_frames.end(),
                     [](const VmafFrame& frame) { return frame.phone.has_value(); })) {
        output.phone_available = false;
        output.warnings.emplace_back(
            "Phone VMAF was requested but not emitted; it is marked unavailable.");
    }
    const std::size_t scene_count = 1 + static_cast<std::size_t>(std::count_if(
        output.scene_scores.begin() + static_cast<std::ptrdiff_t>(
            std::min<std::size_t>(1, output.scene_scores.size())),
        output.scene_scores.end(),
        [&](double score) { return score >= options.scene_threshold; }));
    progress.update("quality_metrics", common_duration, 95.0, output.hashes.size(),
                    scene_count, command, 0.0, 0.0, 0.0, true, {}, true);
    return output;
}

struct FrameMetrics {
    std::size_t frame = 0;
    int scene = 0;
    double time_seconds = 0.0;
    double vmaf_standard = 0.0;
    std::optional<double> vmaf_phone;
    double psnr_y = 0.0;
    double ssim = 0.0;
    double psnr_normalized = 0.0;
    double ssim_normalized = 0.0;
    double phash_similarity = 0.0;
    double temporal_consistency = 0.0;
    double composite = 0.0;
    double reference_change = 0.0;
    double scene_change_score = 0.0;
};

double normalize_psnr(double value) {
    return clamp_score((value - 20.0) / 30.0 * 100.0);
}

double normalize_ssim(double value) {
    return clamp_score(value * 100.0);
}

double composite_score(double standard, double ssim_normalized,
                       double psnr_normalized, double phash) {
    return clamp_score(
        0.50 * clamp_score(standard) +
        0.20 * clamp_score(ssim_normalized) +
        0.15 * clamp_score(psnr_normalized) +
        0.15 * clamp_score(phash));
}

struct SceneRange {
    std::size_t start = 0;
    std::size_t end = 0;
    double change_strength = 0.0;
};

std::vector<SceneRange> detect_scenes(
    std::size_t frame_count, const std::vector<double>& scene_scores,
    double threshold, std::size_t minimum_frames
) {
    std::vector<SceneRange> scenes;
    std::size_t start = 0;
    for (std::size_t index = 1; index < frame_count && index < scene_scores.size(); ++index) {
        if (scene_scores[index] >= threshold) {
            scenes.push_back({start, index, start ? scene_scores[start] : 0.0});
            start = index;
        }
    }
    scenes.push_back({
        start, frame_count,
        start < scene_scores.size() ? scene_scores[start] : 0.0
    });

    while (scenes.size() > 1) {
        auto short_scene = std::find_if(scenes.begin(), scenes.end(), [&](const SceneRange& scene) {
            return scene.end - scene.start < minimum_frames;
        });
        if (short_scene == scenes.end()) break;
        const std::size_t index = static_cast<std::size_t>(short_scene - scenes.begin());
        if (index == 0) {
            scenes[1].start = scenes[0].start;
            scenes[1].change_strength = 0.0;
            scenes.erase(scenes.begin());
        } else if (index + 1 == scenes.size()) {
            scenes[index - 1].end = scenes[index].end;
            scenes.erase(scenes.begin() + static_cast<std::ptrdiff_t>(index));
        } else {
            const double boundary_before = scenes[index].change_strength;
            const double boundary_after = scenes[index + 1].change_strength;
            if (boundary_before <= boundary_after) {
                scenes[index - 1].end = scenes[index].end;
                scenes.erase(scenes.begin() + static_cast<std::ptrdiff_t>(index));
            } else {
                scenes[index + 1].start = scenes[index].start;
                scenes[index + 1].change_strength = scenes[index].change_strength;
                scenes.erase(scenes.begin() + static_cast<std::ptrdiff_t>(index));
            }
        }
    }
    return scenes;
}

double mean(const std::vector<double>& values) {
    if (values.empty()) return std::numeric_limits<double>::quiet_NaN();
    return std::accumulate(values.begin(), values.end(), 0.0) /
           static_cast<double>(values.size());
}

double worst_decile(const std::vector<double>& values) {
    if (values.empty()) return std::numeric_limits<double>::quiet_NaN();
    std::vector<double> sorted = values;
    std::sort(sorted.begin(), sorted.end());
    const std::size_t count = std::max<std::size_t>(
        1, static_cast<std::size_t>(std::ceil(static_cast<double>(sorted.size()) * 0.10)));
    return std::accumulate(sorted.begin(), sorted.begin() + static_cast<std::ptrdiff_t>(count), 0.0) /
           static_cast<double>(count);
}

struct MetricSummary {
    std::optional<double> mean;
    std::optional<double> worst;
};

MetricSummary summarize(const std::vector<double>& values) {
    if (values.empty()) return {};
    return {mean(values), worst_decile(values)};
}

struct SceneMetrics {
    SceneRange range;
    MetricSummary vmaf_standard;
    MetricSummary vmaf_phone;
    MetricSummary psnr_y;
    MetricSummary ssim;
    MetricSummary psnr_normalized;
    MetricSummary ssim_normalized;
    MetricSummary phash;
    MetricSummary temporal;
    MetricSummary composite;
    double score = 0.0;
};

std::string quality_band(double score) {
    if (score >= 90.0) return "Excellent";
    if (score >= 80.0) return "Very good";
    if (score >= 70.0) return "Good";
    if (score >= 55.0) return "Fair";
    return "Poor";
}

std::vector<std::size_t> timeline_indices(const std::vector<FrameMetrics>& frames,
                                          std::size_t maximum = 1000) {
    std::vector<std::size_t> indices;
    if (frames.empty()) return indices;
    const std::size_t buckets = std::min(maximum, frames.size());
    indices.reserve(buckets);
    for (std::size_t bucket = 0; bucket < buckets; ++bucket) {
        const std::size_t start = bucket * frames.size() / buckets;
        const std::size_t end = std::max(start + 1, (bucket + 1) * frames.size() / buckets);
        std::size_t selected = start;
        for (std::size_t index = start + 1; index < end && index < frames.size(); ++index) {
            if (frames[index].composite < frames[selected].composite) selected = index;
        }
        indices.push_back(selected);
    }
    return indices;
}

SceneMetrics summarize_scene(const SceneRange& range, const std::vector<FrameMetrics>& frames) {
    SceneMetrics summary;
    summary.range = range;
    std::vector<double> standard, phone, psnr, ssim, psnr_norm, ssim_norm;
    std::vector<double> phash, temporal, composite;
    for (std::size_t index = range.start; index < range.end && index < frames.size(); ++index) {
        const auto& frame = frames[index];
        standard.push_back(frame.vmaf_standard);
        if (frame.vmaf_phone) phone.push_back(*frame.vmaf_phone);
        psnr.push_back(frame.psnr_y);
        ssim.push_back(frame.ssim);
        psnr_norm.push_back(frame.psnr_normalized);
        ssim_norm.push_back(frame.ssim_normalized);
        phash.push_back(frame.phash_similarity);
        temporal.push_back(frame.temporal_consistency);
        composite.push_back(frame.composite);
    }
    summary.vmaf_standard = summarize(standard);
    summary.vmaf_phone = summarize(phone);
    summary.psnr_y = summarize(psnr);
    summary.ssim = summarize(ssim);
    summary.psnr_normalized = summarize(psnr_norm);
    summary.ssim_normalized = summarize(ssim_norm);
    summary.phash = summarize(phash);
    summary.temporal = summarize(temporal);
    summary.composite = summarize(composite);
    if (summary.composite.mean && summary.composite.worst) {
        summary.score = 0.70 * *summary.composite.mean + 0.30 * *summary.composite.worst;
    }
    return summary;
}

void append_metric_summary_json(std::ostringstream& json, const std::string& name,
                                const MetricSummary& summary, bool comma = true) {
    json << "      \"" << name << "\": {\"mean\": " << optional_number(summary.mean)
         << ", \"worst_decile\": " << optional_number(summary.worst) << "}";
    if (comma) json << ',';
    json << '\n';
}

std::string build_report_json(
    const Options& options, const VideoProbe& reference, const VideoProbe& distorted,
    const Normalization& normalization, double duration, const std::vector<FrameMetrics>& frames,
    const std::vector<SceneMetrics>& scenes, const SceneMetrics& overall,
    bool phone_available, const std::vector<std::string>& warnings
) {
    std::ostringstream json;
    json << "{\n"
         << "  \"schema_version\": 1,\n"
         << "  \"analyzer_version\": \"" << kAnalyzerVersion << "\",\n"
         << "  \"analyzer\": {\"name\": \"" << kAnalyzerName << "\", \"version\": \""
         << kAnalyzerVersion << "\", \"config_version\": \"" << kConfigVersion << "\"},\n"
         << "  \"generated_at\": \"" << utc_now() << "\",\n"
         << "  \"inputs\": {\n"
         << "    \"reference\": \"" << json_escape(options.reference.filename().string()) << "\",\n"
         << "    \"distorted\": \"" << json_escape(options.distorted.filename().string()) << "\"\n"
         << "  },\n"
         << "  \"settings\": {\n"
         << "    \"threads\": " << options.threads << ",\n"
         << "    \"fps\": " << format_number(options.frame_rate, 3) << ",\n"
         << "    \"scene_threshold\": " << format_number(options.scene_threshold, 3) << ",\n"
         << "    \"min_scene_seconds\": " << format_number(options.min_scene_seconds, 3) << ",\n"
         << "    \"normalization\": \"common_bt709_display\"\n"
         << "  },\n"
         << "  \"video\": {\n"
         << "    \"width\": " << distorted.width << ", \"height\": " << distorted.height << ",\n"
         << "    \"duration_seconds\": " << format_number(duration, 3) << ",\n"
         << "    \"frames_analyzed\": " << frames.size() << ",\n"
         << "    \"reference_source_fps\": " << format_number(reference.frame_rate, 3) << ",\n"
         << "    \"distorted_source_fps\": " << format_number(distorted.frame_rate, 3) << "\n"
         << "  },\n"
         << "  \"normalization\": {\n"
         << "    \"reference\": \"" << json_escape(normalization.reference_description) << "\",\n"
         << "    \"distorted\": \"" << json_escape(normalization.distorted_description) << "\",\n"
         << "    \"reference_transfer\": \"" << json_escape(reference.color_transfer) << "\",\n"
         << "    \"distorted_transfer\": \"" << json_escape(distorted.color_transfer) << "\"\n"
         << "  },\n"
         << "  \"hdr_normalized\": " << (normalization.uses_hdr_tonemap ? "true" : "false") << ",\n"
         << "  \"capabilities\": {\n"
         << "    \"libvmaf_standard\": true,\n"
         << "    \"libvmaf_phone\": " << (phone_available ? "true" : "false") << ",\n"
         << "    \"psnr\": true, \"ssim\": true,\n"
         << "    \"hdr_normalization\": " << (normalization.uses_hdr_tonemap ? "true" : "false") << "\n"
         << "  },\n"
         << "  \"weights\": {\n"
         << "    \"vmaf_standard\": 0.50, \"ssim_normalized\": 0.20,\n"
         << "    \"psnr_normalized\": 0.15, \"phash_similarity\": 0.15,\n"
         << "    \"mean\": 0.70, \"worst_decile\": 0.30\n"
         << "  },\n"
         << "  \"summary\": {\n"
         << "    \"score\": " << format_number(overall.score) << ",\n"
         << "    \"band\": \"" << quality_band(overall.score) << "\",\n"
         << "    \"weighted_mean\": " << optional_number(overall.composite.mean) << ",\n"
         << "    \"worst_decile\": " << optional_number(overall.composite.worst) << ",\n"
         << "    \"vmaf_standard\": " << optional_number(overall.vmaf_standard.mean) << ",\n"
         << "    \"vmaf_phone\": " << optional_number(overall.vmaf_phone.mean) << ",\n"
         << "    \"psnr_y\": " << optional_number(overall.psnr_y.mean) << ",\n"
         << "    \"psnr_normalized\": " << optional_number(overall.psnr_normalized.mean) << ",\n"
         << "    \"ssim\": " << optional_number(overall.ssim.mean) << ",\n"
         << "    \"ssim_normalized\": " << optional_number(overall.ssim_normalized.mean) << ",\n"
         << "    \"phash_similarity\": " << optional_number(overall.phash.mean) << ",\n"
         << "    \"temporal_consistency\": " << optional_number(overall.temporal.mean) << "\n"
         << "  },\n"
         << "  \"metrics\": {\n";
    append_metric_summary_json(json, "vmaf_standard", overall.vmaf_standard);
    json << "      \"vmaf_phone\": {\"available\": " << (phone_available ? "true" : "false")
         << ", \"mean\": " << optional_number(overall.vmaf_phone.mean)
         << ", \"worst_decile\": " << optional_number(overall.vmaf_phone.worst) << "},\n";
    append_metric_summary_json(json, "psnr_y", overall.psnr_y);
    append_metric_summary_json(json, "ssim", overall.ssim);
    append_metric_summary_json(json, "psnr_normalized", overall.psnr_normalized);
    append_metric_summary_json(json, "ssim_normalized", overall.ssim_normalized);
    append_metric_summary_json(json, "phash_similarity", overall.phash);
    append_metric_summary_json(json, "temporal_consistency", overall.temporal);
    json << "      \"composite\": {\"weighted_mean\": " << optional_number(overall.composite.mean)
         << ", \"worst_decile\": " << optional_number(overall.composite.worst)
         << ", \"score\": " << format_number(overall.score) << "}\n"
         << "  },\n";
    const auto sampled = timeline_indices(frames);
    json << "  \"timeline\": [\n";
    for (std::size_t position = 0; position < sampled.size(); ++position) {
        const auto& frame = frames[sampled[position]];
        json << "    {\"frame\": " << frame.frame
             << ", \"time_seconds\": " << format_number(frame.time_seconds, 6)
             << ", \"scene\": " << frame.scene
             << ", \"score\": " << format_number(frame.composite, 6)
             << ", \"composite\": " << format_number(frame.composite, 6)
             << ", \"vmaf_standard\": " << format_number(frame.vmaf_standard, 6)
             << ", \"vmaf_phone\": " << optional_number(frame.vmaf_phone, 6)
             << ", \"psnr_y\": " << format_number(frame.psnr_y, 6)
             << ", \"psnr_normalized\": " << format_number(frame.psnr_normalized, 6)
             << ", \"ssim\": " << format_number(frame.ssim, 9)
             << ", \"ssim_normalized\": " << format_number(frame.ssim_normalized, 6)
             << ", \"phash_similarity\": " << format_number(frame.phash_similarity, 6)
             << ", \"temporal_consistency\": "
             << format_number(frame.temporal_consistency, 6) << "}"
             << (position + 1 == sampled.size() ? "" : ",") << "\n";
    }
    json << "  ],\n"
         << "  \"frames\": [\n";
    for (std::size_t index = 0; index < frames.size(); ++index) {
        const auto& frame = frames[index];
        json << "    {\"frame\": " << frame.frame
             << ", \"time_seconds\": " << format_number(frame.time_seconds, 6)
             << ", \"scene\": " << frame.scene
             << ", \"vmaf_standard\": " << format_number(frame.vmaf_standard, 6)
             << ", \"vmaf_phone\": " << optional_number(frame.vmaf_phone, 6)
             << ", \"psnr_y\": " << format_number(frame.psnr_y, 6)
             << ", \"psnr_normalized\": " << format_number(frame.psnr_normalized, 6)
             << ", \"ssim\": " << format_number(frame.ssim, 9)
             << ", \"ssim_normalized\": " << format_number(frame.ssim_normalized, 6)
             << ", \"phash_similarity\": " << format_number(frame.phash_similarity, 6)
             << ", \"temporal_consistency\": "
             << format_number(frame.temporal_consistency, 6)
             << ", \"composite\": " << format_number(frame.composite, 6)
             << ", \"reference_phash_change\": "
             << format_number(frame.reference_change, 6)
             << ", \"source_scene_score\": "
             << format_number(frame.scene_change_score, 6) << "}"
             << (index + 1 == frames.size() ? "" : ",") << "\n";
    }
    json << "  ],\n"
         << "  \"scenes\": [\n";
    for (std::size_t index = 0; index < scenes.size(); ++index) {
        const auto& scene = scenes[index];
        json << "    {\n"
             << "      \"index\": " << index + 1 << ",\n"
             << "      \"start_frame\": " << scene.range.start << ", \"end_frame\": "
             << scene.range.end << ",\n"
             << "      \"start_seconds\": "
             << format_number(scene.range.start / options.frame_rate)
             << ", \"end_seconds\": "
             << format_number(scene.range.end / options.frame_rate) << ",\n"
             << "      \"duration_seconds\": "
             << format_number((scene.range.end - scene.range.start) / options.frame_rate) << ",\n"
             << "      \"frame_count\": " << scene.range.end - scene.range.start << ",\n"
             << "      \"scene_change_strength\": " << format_number(scene.range.change_strength) << ",\n"
             << "      \"score\": " << format_number(scene.score) << ",\n"
             << "      \"band\": \"" << quality_band(scene.score) << "\",\n"
             << "      \"metrics\": {\n";
        append_metric_summary_json(json, "vmaf_standard", scene.vmaf_standard);
        json << "      \"vmaf_phone\": {\"available\": " << (phone_available ? "true" : "false")
             << ", \"mean\": " << optional_number(scene.vmaf_phone.mean)
             << ", \"worst_decile\": " << optional_number(scene.vmaf_phone.worst) << "},\n";
        append_metric_summary_json(json, "psnr_y", scene.psnr_y);
        append_metric_summary_json(json, "ssim", scene.ssim);
        append_metric_summary_json(json, "psnr_normalized", scene.psnr_normalized);
        append_metric_summary_json(json, "ssim_normalized", scene.ssim_normalized);
        append_metric_summary_json(json, "phash_similarity", scene.phash);
        append_metric_summary_json(json, "temporal_consistency", scene.temporal);
        json << "      \"composite\": {\"weighted_mean\": " << optional_number(scene.composite.mean)
             << ", \"worst_decile\": " << optional_number(scene.composite.worst)
             << ", \"score\": " << format_number(scene.score) << "}\n"
             << "      }\n"
             << "    }" << (index + 1 == scenes.size() ? "" : ",") << "\n";
    }
    json << "  ],\n"
         << "  \"artifacts\": {\"frames_csv\": \"frames.csv\", \"html_report\": \"report.html\"},\n"
         << "  \"warnings\": [";
    for (std::size_t index = 0; index < warnings.size(); ++index) {
        if (index) json << ", ";
        json << "\"" << json_escape(warnings[index]) << "\"";
    }
    json << "]\n}\n";
    return json.str();
}

std::string build_frames_csv(const std::vector<FrameMetrics>& frames) {
    std::ostringstream csv;
    csv << "frame,time_seconds,scene,vmaf_standard,vmaf_phone,psnr_y,psnr_normalized,"
           "ssim,ssim_normalized,phash_similarity,temporal_consistency,composite,"
           "reference_phash_change,source_scene_score\n";
    for (const auto& frame : frames) {
        csv << frame.frame << ',' << format_number(frame.time_seconds, 6) << ','
            << frame.scene << ',' << format_number(frame.vmaf_standard, 6) << ',';
        if (frame.vmaf_phone) csv << format_number(*frame.vmaf_phone, 6);
        csv << ',' << format_number(frame.psnr_y, 6) << ','
            << format_number(frame.psnr_normalized, 6) << ','
            << format_number(frame.ssim, 9) << ','
            << format_number(frame.ssim_normalized, 6) << ','
            << format_number(frame.phash_similarity, 6) << ','
            << format_number(frame.temporal_consistency, 6) << ','
            << format_number(frame.composite, 6) << ','
            << format_number(frame.reference_change, 6) << ','
            << format_number(frame.scene_change_score, 6) << '\n';
    }
    return csv.str();
}

std::string metric_card(const std::string& label, const std::optional<double>& value,
                        const std::string& suffix = {}) {
    std::ostringstream html;
    html << "<div class=\"metric\"><span>" << html_escape(label) << "</span><strong>";
    if (value) html << std::fixed << std::setprecision(2) << *value << html_escape(suffix);
    else html << "N/A";
    html << "</strong></div>";
    return html.str();
}

std::string build_quality_chart(const std::vector<FrameMetrics>& frames) {
    const auto sampled = timeline_indices(frames);
    if (sampled.empty()) return {};
    std::ostringstream points;
    for (std::size_t index = 0; index < sampled.size(); ++index) {
        const double x = sampled.size() == 1
            ? 0.0 : static_cast<double>(index) * 1000.0 /
                    static_cast<double>(sampled.size() - 1);
        const double y = 10.0 + (100.0 - frames[sampled[index]].composite) * 2.0;
        if (index) points << ' ';
        points << std::fixed << std::setprecision(2) << x << ',' << y;
    }
    std::ostringstream html;
    html << "<section><h2>Quality over time</h2>"
         << "<p class=\"sub\">Up to 1,000 minimum-preserving time buckets; downward spikes "
            "show the weakest frames.</p>"
         << "<div class=\"chart\"><svg role=\"img\" aria-label=\"Composite quality over time\" "
            "viewBox=\"0 0 1000 220\" preserveAspectRatio=\"none\">"
         << "<line x1=\"0\" y1=\"30\" x2=\"1000\" y2=\"30\" class=\"guide\"/>"
         << "<line x1=\"0\" y1=\"110\" x2=\"1000\" y2=\"110\" class=\"guide\"/>"
         << "<line x1=\"0\" y1=\"210\" x2=\"1000\" y2=\"210\" class=\"guide\"/>"
         << "<polyline points=\"" << points.str() << "\"/></svg>"
         << "<div class=\"axis\"><span>Start</span><span>End</span></div></div></section>";
    return html.str();
}

std::string build_report_html(
    const Options& options, double duration, const std::vector<FrameMetrics>& frames,
    const std::vector<SceneMetrics>& scenes, const SceneMetrics& overall,
    bool phone_available, const std::vector<std::string>& warnings
) {
    std::ostringstream html;
    html << "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
         << "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
         << "<title>Video quality report</title><style>"
         << ":root{color-scheme:dark;--bg:#0d1117;--card:#161b22;--line:#30363d;"
            "--text:#f0f6fc;--muted:#8b949e;--accent:#f778ba;--good:#3fb950}"
         << "*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0,"
            "#2b1730 0,transparent 35%),var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif}"
         << "main{max-width:1180px;margin:auto;padding:38px 22px 70px}h1{font-size:clamp(2rem,5vw,4rem);"
            "margin:.1em 0}.kicker{color:var(--accent);font-weight:800;letter-spacing:.12em;text-transform:uppercase}"
         << ".sub,.note{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));"
            "gap:12px;margin:24px 0}.metric{background:linear-gradient(145deg,#1b2028,var(--card));"
            "border:1px solid var(--line);border-radius:14px;padding:17px}.metric span{display:block;color:var(--muted);"
            "font-size:.78rem;text-transform:uppercase;letter-spacing:.08em}.metric strong{display:block;font-size:1.8rem;"
            "margin-top:5px}.score strong{color:var(--good)}section{margin-top:34px}"
         << ".table{overflow:auto;border:1px solid var(--line);border-radius:14px;background:var(--card)}"
            "table{width:100%;border-collapse:collapse;white-space:nowrap}th,td{padding:12px 14px;text-align:right;"
            "border-bottom:1px solid var(--line)}th:first-child,td:first-child{text-align:left}th{color:var(--muted);"
            "font-size:.75rem;text-transform:uppercase;letter-spacing:.06em}tr:last-child td{border:0}"
         << ".warnings{border-left:4px solid #d29922;background:#2b2417;padding:12px 18px;border-radius:8px}"
            ".chart{background:#10151c;border:1px solid var(--line);border-radius:14px;padding:12px}"
            ".chart svg{display:block;width:100%;height:260px}.chart polyline{fill:none;stroke:var(--accent);"
            "stroke-width:3;vector-effect:non-scaling-stroke}.chart .guide{stroke:#30363d;stroke-width:1}"
            ".axis{display:flex;justify-content:space-between;color:var(--muted);font-size:.8rem}"
            "code{color:#ffa7d1}a{color:#ff9bd2}</style></head><body><main>"
         << "<p class=\"kicker\">Objective video analysis</p><h1>Quality report</h1>"
         << "<p class=\"sub\"><code>" << html_escape(options.distorted.filename().string())
         << "</code> compared with <code>" << html_escape(options.reference.filename().string())
         << "</code> · " << std::fixed << std::setprecision(2) << duration
         << " seconds · " << frames.size() << " aligned frames at "
         << std::setprecision(3) << options.frame_rate << " fps</p><div class=\"grid\">";
    html << "<div class=\"metric score\"><span>Overall score</span><strong>"
         << std::fixed << std::setprecision(2) << overall.score << "</strong><small>"
         << quality_band(overall.score) << "</small></div>";
    html << metric_card("Standard VMAF", overall.vmaf_standard.mean)
         << metric_card("Phone VMAF", overall.vmaf_phone.mean)
         << metric_card("PSNR Y", overall.psnr_y.mean, " dB")
         << metric_card("SSIM", overall.ssim.mean)
         << metric_card("pHash similarity", overall.phash.mean)
         << metric_card("Temporal consistency", overall.temporal.mean)
         << "</div><p class=\"note\">Composite: 50% standard VMAF, 20% normalized SSIM, "
            "15% normalized PSNR, and 15% pHash similarity. The final score is 70% "
            "weighted mean plus 30% worst decile. Phone VMAF and temporal consistency "
            "are informational diagnostics.</p>";
    if (!warnings.empty()) {
        html << "<div class=\"warnings\"><strong>Warnings</strong><ul>";
        for (const auto& warning : warnings) html << "<li>" << html_escape(warning) << "</li>";
        html << "</ul></div>";
    }
    html << build_quality_chart(frames);
    html << "<section><h2>Per-scene results</h2><p class=\"sub\">Scene boundaries come only "
            "from FFmpeg scdet on the normalized reference; clips shorter than the requested minimum are merged.</p>"
         << "<div class=\"table\"><table><thead><tr><th>Scene</th><th>Range</th><th>Frames</th>"
            "<th>Score</th><th>VMAF</th><th>Phone</th><th>PSNR Y</th><th>SSIM</th><th>pHash</th>"
            "<th>Temporal</th></tr></thead><tbody>";
    auto show = [](const std::optional<double>& value, int precision = 2) {
        if (!value) return std::string("N/A");
        std::ostringstream out;
        out << std::fixed << std::setprecision(precision) << *value;
        return out.str();
    };
    for (std::size_t index = 0; index < scenes.size(); ++index) {
        const auto& scene = scenes[index];
        html << "<tr><td>Scene " << index + 1 << "</td><td>"
             << std::fixed << std::setprecision(2) << scene.range.start / options.frame_rate
             << "–" << scene.range.end / options.frame_rate << "s</td><td>"
             << scene.range.end - scene.range.start << "</td><td><strong>"
             << std::setprecision(2) << scene.score << "</strong></td><td>"
             << show(scene.vmaf_standard.mean) << "</td><td>" << show(scene.vmaf_phone.mean)
             << "</td><td>" << show(scene.psnr_y.mean) << "</td><td>"
             << show(scene.ssim.mean, 4) << "</td><td>" << show(scene.phash.mean)
             << "</td><td>" << show(scene.temporal.mean) << "</td></tr>";
    }
    html << "</tbody></table></div></section><section><h2>Artifacts</h2>"
            "<p><a href=\"report.json\">report.json</a> · "
            "<a href=\"frames.csv\">frames.csv</a></p></section>"
         << "<footer class=\"note\">Generated by " << kAnalyzerName << " " << kAnalyzerVersion
         << " · phone model " << (phone_available ? "available" : "unavailable")
         << "</footer></main></body></html>\n";
    return html.str();
}

void print_terminal_summary(const SceneMetrics& overall, std::size_t frames,
                            const std::vector<SceneMetrics>& scenes, double duration,
                            bool phone_available, const fs::path& output_directory,
                            double frame_rate) {
    auto value = [](const std::optional<double>& number) {
        if (!number) return std::string("N/A");
        std::ostringstream out;
        out << std::fixed << std::setprecision(2) << *number;
        return out.str();
    };
    std::cout << "\nVideo quality analysis complete\n"
              << "  Overall score:       " << std::fixed << std::setprecision(2)
              << overall.score << " / 100 (" << quality_band(overall.score) << ")\n"
              << "  Standard VMAF:       " << value(overall.vmaf_standard.mean) << "\n"
              << "  Phone VMAF:          " << value(overall.vmaf_phone.mean)
              << (phone_available ? "" : " (unavailable)") << "\n"
              << "  PSNR Y:              " << value(overall.psnr_y.mean) << " dB\n"
              << "  SSIM:                " << value(overall.ssim.mean) << "\n"
              << "  pHash similarity:    " << value(overall.phash.mean) << " / 100\n"
              << "  Temporal consistency:" << ' ' << value(overall.temporal.mean) << " / 100\n"
              << "  Coverage:            " << frames << " frames, " << scenes.size()
              << " scenes, " << std::setprecision(2) << duration << " seconds\n"
              << "  Reports:             " << fs::absolute(output_directory).string() << "\n";
    std::vector<std::size_t> weakest(scenes.size());
    std::iota(weakest.begin(), weakest.end(), 0);
    std::stable_sort(weakest.begin(), weakest.end(), [&](std::size_t left, std::size_t right) {
        return scenes[left].score < scenes[right].score;
    });
    if (!weakest.empty()) {
        std::cout << "  Weakest scenes:\n";
        for (std::size_t rank = 0; rank < std::min<std::size_t>(5, weakest.size()); ++rank) {
            const std::size_t index = weakest[rank];
            const auto& scene = scenes[index];
            std::cout << "    #" << index + 1 << "  " << std::fixed << std::setprecision(2)
                      << scene.range.start / frame_rate << "–"
                      << scene.range.end / frame_rate << "s  "
                      << scene.score << " (" << quality_band(scene.score) << ")\n";
        }
    }
}

int run(const Options& options) {
    if (!fs::is_regular_file(options.reference))
        throw Error("Reference is not a readable regular file: " + options.reference.string());
    if (!fs::is_regular_file(options.distorted))
        throw Error("Distorted input is not a readable regular file: " + options.distorted.string());
    fs::create_directories(options.output_directory);
    if (!fs::is_directory(options.output_directory))
        throw Error("Output path is not a directory: " + options.output_directory.string());

    const VideoProbe reference = probe_video(options.reference);
    const VideoProbe distorted = probe_video(options.distorted);
    const double common_duration = std::min(reference.duration, distorted.duration);
    const std::size_t expected_frames = std::max<std::size_t>(
        1, static_cast<std::size_t>(std::floor(common_duration * options.frame_rate)));
    ProgressWriter progress(options, common_duration, expected_frames);
    progress.update("probing", 0.0, 0.0, 0, 0, {}, 0.0, 0.0, 0.0, true, {}, true);

    const Normalization normalization = build_normalization(reference);
    std::vector<std::string> warnings;
    if (std::abs(reference.duration - distorted.duration) > 1.0 / options.frame_rate) {
        warnings.emplace_back("Input durations differ; analysis stops at the shorter aligned stream.");
    }
    if (reference.width != distorted.width || reference.height != distorted.height) {
        warnings.emplace_back(
            "Reference dimensions differ and were scaled to the distorted encoded display size.");
    }
    if (reference.color_transfer != distorted.color_transfer ||
        reference.color_primaries != distorted.color_primaries ||
        reference.color_space != distorted.color_space) {
        warnings.emplace_back(
            "Input color metadata differs; both streams used the reference-derived display transform.");
    }

    auto combined = analyze_combined(
        options, distorted, normalization, progress, common_duration, expected_frames);
    warnings.insert(warnings.end(), combined.warnings.begin(), combined.warnings.end());

    const std::size_t frame_count = std::min({
        combined.hashes.size(), combined.vmaf_frames.size(), combined.scene_scores.size()
    });
    if (frame_count == 0) throw Error("No common pHash/libvmaf frames were produced");
    if (combined.hashes.size() != combined.vmaf_frames.size() ||
        combined.hashes.size() != combined.scene_scores.size()) {
        warnings.emplace_back(
            "Metric branches returned different frame counts; reports use their common prefix.");
    }
    const std::size_t minimum_scene_frames = std::max<std::size_t>(
        1, static_cast<std::size_t>(
               std::llround(options.min_scene_seconds * options.frame_rate)));
    const auto ranges = detect_scenes(
        frame_count, combined.scene_scores, options.scene_threshold, minimum_scene_frames);

    std::vector<FrameMetrics> frames;
    frames.reserve(frame_count);
    for (std::size_t index = 0; index < frame_count; ++index) {
        const auto& vf = combined.vmaf_frames[index];
        const auto& hf = combined.hashes[index];
        FrameMetrics frame;
        frame.frame = index;
        frame.time_seconds = static_cast<double>(index) / options.frame_rate;
        frame.vmaf_standard = clamp_score(vf.standard.value_or(0.0));
        if (combined.phone_available && vf.phone) frame.vmaf_phone = clamp_score(*vf.phone);
        frame.psnr_y = vf.psnr_y.value_or(0.0);
        frame.ssim = vf.ssim.value_or(0.0);
        frame.psnr_normalized = normalize_psnr(frame.psnr_y);
        frame.ssim_normalized = normalize_ssim(frame.ssim);
        frame.phash_similarity = hf.similarity;
        frame.temporal_consistency =
            index == 0 || combined.scene_scores[index] >= options.scene_threshold
                ? 100.0 : hf.temporal_consistency;
        frame.reference_change = hf.reference_change;
        frame.scene_change_score = combined.scene_scores[index];
        frame.composite = composite_score(
            frame.vmaf_standard, frame.ssim_normalized,
            frame.psnr_normalized, frame.phash_similarity);
        frames.push_back(frame);
    }
    for (std::size_t scene_index = 0; scene_index < ranges.size(); ++scene_index) {
        for (std::size_t frame = ranges[scene_index].start;
             frame < ranges[scene_index].end && frame < frames.size(); ++frame) {
            frames[frame].scene = static_cast<int>(scene_index + 1);
        }
    }

    std::vector<SceneMetrics> scenes;
    scenes.reserve(ranges.size());
    for (const auto& range : ranges) scenes.push_back(summarize_scene(range, frames));
    const SceneRange all_frames{0, frames.size(), 0.0};
    const SceneMetrics overall = summarize_scene(all_frames, frames);

    progress.update("writing_reports", common_duration, 98.0, frames.size(), scenes.size(),
                    {}, 0.0, 0.0, 0.0, true, {}, true);
    atomic_write(options.output_directory / "frames.csv", build_frames_csv(frames));
    atomic_write(options.output_directory / "report.html",
        build_report_html(options, common_duration, frames, scenes, overall,
                          combined.phone_available, warnings));
    // report.json is the commit marker: consumers never observe it before the
    // same-generation CSV and HTML artifacts have been published.
    atomic_write(options.output_directory / "report.json",
        build_report_json(options, reference, distorted, normalization, common_duration,
                          frames, scenes, overall, combined.phone_available, warnings));

    progress.update("complete", common_duration, 100.0, frames.size(), scenes.size(),
                    {}, 0.0, 0.0, 0.0, false, {}, true);
    print_terminal_summary(overall, frames.size(), scenes, common_duration,
                           combined.phone_available, options.output_directory,
                           options.frame_rate);
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    std::optional<Options> options;
    try {
        options = parse_arguments(argc, argv);
        return run(*options);
    } catch (const std::exception& error) {
        std::cerr << "quality analyzer: " << error.what() << "\n";
        if (options && options->progress_json) {
            try {
                const std::string json =
                    "{\n  \"schema_version\": 1,\n  \"analyzer_version\": \"" +
                    std::string(kAnalyzerVersion) + "\",\n  \"config_version\": \"" +
                    std::string(kConfigVersion) +
                    "\",\n  \"active\": false,\n  \"phase\": \"failed\",\n"
                    "  \"percent\": 0,\n  \"updated_at\": \"" + utc_now() +
                    "\",\n  \"error\": \"" + json_escape(error.what()) + "\"\n}\n";
                atomic_write(*options->progress_json, json);
            } catch (...) {
                // Preserve the original failure.
            }
        }
        return 2;
    }
}
