// Exact decisions concern the supplied binary64 coordinates and triangle indices.
// No repair, orientation change, proximity welding or remeshing is performed.
#include <CGAL/Exact_predicates_exact_constructions_kernel.h>
#include <CGAL/Surface_mesh.h>
#include <CGAL/Polygon_mesh_processing/self_intersections.h>
#include <CGAL/Polygon_mesh_processing/orientation.h>
#include <CGAL/Polygon_mesh_processing/manifoldness.h>
#include <CGAL/Polygon_mesh_processing/connected_components.h>
#include <CGAL/Box_intersection_d/Box_d.h>
#include <CGAL/box_intersection_d.h>
#include <CGAL/version.h>
#include <array>
#include <vector>
#include <iostream>
#include <iomanip>
#include <limits>
#include <cmath>
#include <sstream>
#include <stdexcept>

using K = CGAL::Exact_predicates_exact_constructions_kernel;
using Point = K::Point_3;
using Triangle = std::array<std::size_t, 3>;
using Mesh = CGAL::Surface_mesh<Point>;
namespace PMP = CGAL::Polygon_mesh_processing;
using Box = CGAL::Box_intersection_d::Box_d<double, 3>;
constexpr std::size_t MAX_FACES = 100000, MAX_VERTICES = 300000, MAX_PAIRS = 2000000;

void interval(const K::FT& v) {
  auto bound = CGAL::to_interval(v);
  if (!std::isfinite(bound.first) || !std::isfinite(bound.second)) throw std::runtime_error("BUDGET_EXCEEDED");
  std::cout << '[' << bound.first << ',' << bound.second << ']';
}

int main(int argc, char** argv) {
  std::cout << std::setprecision(17) << std::boolalpha;
  if (argc == 2 && std::string(argv[1]) == "--build-info") {
    std::cout << "{\"source_hash\":\"" << LLCAD_SOURCE_HASH << "\",\"cgal\":\"" << CGAL_VERSION_STR
              << "\",\"kernel\":\"EPECK\"}\n";
    return 0;
  }
  try {
    std::size_t nv, nf;
    if (!(std::cin >> nv >> nf) || !nv || !nf || nv > MAX_VERTICES || nf > MAX_FACES)
      throw std::runtime_error("BUDGET_EXCEEDED");
    std::vector<Point> points; points.reserve(nv);
    for (std::size_t i = 0; i < nv; ++i) {
      double x, y, z;
      if (!(std::cin >> x >> y >> z) || !std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z) ||
          std::max({std::abs(x), std::abs(y), std::abs(z)}) > 1e12)
        throw std::runtime_error("INVALID_SCHEMA");
      points.emplace_back(x, y, z);
    }
    std::vector<Triangle> triangles(nf);
    for (auto& t : triangles)
      if (!(std::cin >> t[0] >> t[1] >> t[2]) || t[0] >= nv || t[1] >= nv || t[2] >= nv)
        throw std::runtime_error("INVALID_SCHEMA");
    std::string extra;
    if (std::cin >> extra) throw std::runtime_error("INVALID_SCHEMA");

    std::size_t degenerate = 0, pairs = 0;
    std::vector<Box> boxes; boxes.reserve(nf);
    K::FT signed_volume(0);
    double area_lo = 0, area_hi = 0, quality_min = 1, quality_max = 0;
    const auto origin = points.front();
    for (const auto& t : triangles) {
      const auto& a = points[t[0]]; const auto& b = points[t[1]]; const auto& c = points[t[2]];
      auto tri = K::Triangle_3(a, b, c);
      boxes.emplace_back(tri.bbox());
      if (tri.is_degenerate()) ++degenerate;
      signed_volume += CGAL::volume(origin, a, b, c);
      const auto a2 = CGAL::to_interval(tri.squared_area());
      double lo = std::nextafter(std::sqrt(std::max(0.0, a2.first)), 0.0);
      double hi = std::nextafter(std::sqrt(std::max(0.0, a2.second)), std::numeric_limits<double>::infinity());
      area_lo = std::nextafter(area_lo + lo, 0.0);
      area_hi = std::nextafter(area_hi + hi, std::numeric_limits<double>::infinity());
      auto sum = CGAL::squared_distance(a,b) + CGAL::squared_distance(b,c) + CGAL::squared_distance(c,a);
      double q = sum == 0 ? 0 : std::sqrt(std::max(0.0, CGAL::to_double(48 * tri.squared_area() / (sum * sum))));
      quality_min = std::min(quality_min, std::min(1.0, q)); quality_max = std::max(quality_max, std::min(1.0, q));
    }
    // Count every broad-phase pair before exact intersection predicates. This
    // includes adjacent triangles and keeps adversarial overlap bounded.
    CGAL::box_self_intersection_d(boxes.begin(), boxes.end(), [&](const Box&, const Box&) {
      if (++pairs > MAX_PAIRS) throw std::runtime_error("BUDGET_EXCEEDED");
    });
    std::vector<std::pair<std::size_t, std::size_t>> intersections;
    PMP::triangle_soup_self_intersections<CGAL::Sequential_tag>(points, triangles, std::back_inserter(intersections),
      CGAL::parameters::maximum_number(16));

    Mesh mesh; std::vector<Mesh::Vertex_index> vertices;
    for (const auto& p : points) vertices.push_back(mesh.add_vertex(p));
    bool inserted = degenerate == 0;
    if (inserted) for (const auto& t : triangles)
      if (mesh.add_face(vertices[t[0]], vertices[t[1]], vertices[t[2]]) == Mesh::null_face()) { inserted = false; break; }
    bool closed = inserted && CGAL::is_closed(mesh);
    std::vector<Mesh::Halfedge_index> bad_vertices;
    if (inserted) PMP::non_manifold_vertices(mesh, std::back_inserter(bad_vertices));
    const bool volume_checked = closed && bad_vertices.empty() && degenerate == 0 && intersections.empty();
    std::vector<std::size_t> levels, first_triangles;
    std::vector<bool> outward;
    std::vector<PMP::Volume_error_code> errors;
    std::size_t solids = 0, components = 0;
    bool orientation_valid = false;
    if (volume_checked) {
      auto cc = mesh.add_property_map<Mesh::Face_index, std::size_t>("f:cc", 0).first;
      components = PMP::connected_components(mesh, cc);
      if (components > 64) throw std::runtime_error("BUDGET_EXCEEDED");
      auto volumes = mesh.add_property_map<Mesh::Face_index, std::size_t>("f:volume", 0).first;
      PMP::volume_connected_components(mesh, volumes, CGAL::parameters::do_self_intersection_tests(false)
        .do_orientation_tests(true).face_connected_component_map(cc).nesting_levels(std::ref(levels))
        .is_cc_outward_oriented(std::ref(outward)).error_codes(std::ref(errors)));
      if (levels.empty() && components == 1) levels.push_back(0);
      if (levels.size() != components || outward.size() != components) throw std::runtime_error("GEOMETRY_INVALID");
      orientation_valid = levels.size() == components && outward.size() == components && !errors.empty();
      first_triangles.assign(components, nf);
      for (const auto face : mesh.faces()) first_triangles[cc[face]] = std::min(first_triangles[cc[face]], std::size_t(face.idx()));
      for (const auto e : errors) orientation_valid = orientation_valid && e == PMP::VALID_VOLUME;
      for (std::size_t i=0; i<levels.size(); ++i) {
        orientation_valid = orientation_valid && outward[i] == (levels[i] % 2 == 0);
        if (levels[i] % 2 == 0) ++solids;
      }
      orientation_valid = orientation_valid && signed_volume > 0;
    }
    // Nothing is written until all potentially failing native operations finish.
    std::cout << "{\"schema_version\":\"1\",\"engine\":\"CGAL-" << CGAL_VERSION_STR
      << "/EPECK\",\"source_hash\":\"" << LLCAD_SOURCE_HASH << "\",\"degenerate_triangles\":" << degenerate
      << ",\"aabb_candidate_pairs\":" << pairs << ",\"self_intersections_found\":" << !intersections.empty()
      << ",\"intersection_examples\":[";
    for (std::size_t i=0; i<intersections.size(); ++i) { if(i) std::cout << ','; std::cout << '[' << intersections[i].first << ',' << intersections[i].second << ']'; }
    std::cout << "],\"volume_checked\":" << volume_checked << ",\"nested_orientation_valid\":" << orientation_valid
      << ",\"bounded_solids\":" << (orientation_valid ? solids : 0) << ",\"signed_volume_interval_mm3\":";
    interval(signed_volume);
    std::cout << ",\"area_interval_mm2\":[" << area_lo << ',' << area_hi << "],\"triangle_quality_min\":" << quality_min
      << ",\"triangle_quality_max\":" << quality_max << ",\"components\":[";
    for (std::size_t i=0; i<levels.size(); ++i) { if(i) std::cout << ','; std::cout << "{\"first_triangle\":" << first_triangles[i] << ",\"nesting_depth\":" << levels[i] << ",\"outward\":" << outward[i] << '}'; }
    std::cout << "]}\n";
    return 0;
  } catch (const std::exception& error) {
    const std::string code = error.what();
    std::cerr << (code == "INVALID_SCHEMA" || code == "BUDGET_EXCEEDED" ? code : "GEOMETRY_INVALID") << '\n';
    return 2;
  } catch (...) { std::cerr << "GEOMETRY_INVALID\n"; return 2; }
}
