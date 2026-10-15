#include <cstdlib>
#include <iostream>

#include "reboot_deadline.h"

static void check(bool value) {
  if (!value) std::abort();
}

int main() {
  mechadog::RebootDeadline inactive;
  check(!inactive.due(0));
  check(!inactive.due(UINT32_C(0x80000000)));
  check(!inactive.due(UINT32_MAX));

  mechadog::RebootDeadline normal;
  normal.schedule(1000, 500);
  check(!normal.due(1000));
  check(!normal.due(1499));
  check(normal.due(1500));
  check(normal.due(1501));

  // Regression: the old zero sentinel discarded exactly this restart.
  mechadog::RebootDeadline zero;
  zero.schedule(UINT32_MAX - 499, 500);
  check(!zero.due(UINT32_MAX - 499));
  check(!zero.due(UINT32_MAX));
  check(zero.due(0));
  check(zero.due(1));

  mechadog::RebootDeadline wrap;
  wrap.schedule(UINT32_MAX - 249, 500);
  check(!wrap.due(UINT32_MAX - 249));
  check(!wrap.due(UINT32_MAX));
  check(!wrap.due(0));
  check(!wrap.due(249));
  check(wrap.due(250));
  check(wrap.due(251));

  mechadog::RebootDeadline immediate;
  immediate.schedule(0, 0);
  check(immediate.due(0));

  mechadog::RebootDeadline replacement;
  replacement.schedule(1000, 500);
  replacement.schedule(1200, 500);
  check(!replacement.due(1500));
  check(!replacement.due(1699));
  check(replacement.due(1700));

  std::cout << "OTA reboot deadline: inactive, exact expiry, zero deadline, millisecond wrap, "
               "immediate and replacement scheduling passed\n";
}
