import { drizzle } from 'drizzle-orm/d1';
import * as schema from './schema';

export class DatabaseService {
  public readonly db: any;

  constructor(d1: any) {
    this.db = drizzle(d1 as any, { schema });
  }
}
